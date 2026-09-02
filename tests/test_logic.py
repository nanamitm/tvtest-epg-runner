"""The decisions the runner makes, checked without a tuner in the room.

Everything here is pure: durations, channel lists, ordering, the free window
EDCB leaves and the shape of a saved configuration.  What needs a tuner —
launching TVTest, cancelling it, adopting one — is not covered.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, time as clock_time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tvtest_epg_runner import channels, config as config_module
from tvtest_epg_runner.edcb import Reservation, free_until
from tvtest_epg_runner.history import CaptureHistory
from tvtest_epg_runner.scheduler import next_run_after
from tvtest_epg_runner.util import format_duration, parse_duration

CH2 = """\
; コメント行
;#SPACE(0,地デジ)
ＮＨＫ総合,0,4,1,1,1024,32736,32736,1
ＮＨＫEテレ,0,4,2,1,1032,32736,32736,1
無効な局,0,7,3,1,1040,32736,32737,0
;#SPACE(2,BS)
ＮＨＫBS,2,18,101,1,101,4,16625,1
ＢＳ日テレ,2,15,141,1,141,4,16592,1
"""


class DurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("40m"), 2400)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("1800"), 1800)

    def test_rejects_nonsense(self):
        # 列を間違えてチャンネル指定を書いた場合に、数字だけ拾わせない
        for text in ("2:*,3:*", "2:9-1", "abc", "40x"):
            with self.assertRaises(ValueError, msg=text):
                parse_duration(text)

    def test_empty_falls_back(self):
        self.assertEqual(parse_duration("", 7), 7)
        self.assertEqual(parse_duration(None, 7), 7)

    def test_format(self):
        self.assertEqual(format_duration(90), "1分30秒")
        self.assertEqual(format_duration(3600), "1時間")


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "BonDriver_Test.ch2")
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(CH2)
        self.groups = channels.load_groups(self.path)

    def test_groups_by_physical_channel(self):
        # 同じ空間・同じチャンネルのサービスは1つにまとまる
        self.assertEqual([g.key for g in self.groups], ["0:4", "2:15", "2:18"])
        self.assertEqual(len(self.groups[0].services), 2)

    def test_disabled_channels_are_left_out(self):
        self.assertNotIn("0:7", [g.key for g in self.groups])

    def test_shift_jis_file(self):
        path = os.path.join(os.path.dirname(self.path), "BonDriver_Sjis.ch2")
        with open(path, "wb") as file:
            file.write(CH2.encode("cp932"))
        self.assertEqual(len(channels.load_groups(path)), 3)

    def test_utf16_file(self):
        # TVTest が書くのは印付き。印の無いものも黙って0件にしない。
        for name, data in (
            ("BonDriver_Utf16.ch2", CH2.encode("utf-16")),
            ("BonDriver_Utf16Bare.ch2", CH2.encode("utf-16-le")),
        ):
            path = os.path.join(os.path.dirname(self.path), name)
            with open(path, "wb") as file:
                file.write(data)
            self.assertEqual(len(channels.load_groups(path)), 3, name)

    def test_select(self):
        self.assertEqual(len(channels.select(self.groups, "*:*")), 3)
        self.assertEqual([g.key for g in channels.select(self.groups, "2:*")],
                         ["2:15", "2:18"])
        self.assertEqual([g.key for g in channels.select(self.groups, "2:15-18")],
                         ["2:15", "2:18"])
        self.assertEqual([g.key for g in channels.select(self.groups, "0:4")], ["0:4"])

    def test_spec_round_trip(self):
        spec = channels.to_spec(self.groups)
        self.assertEqual(len(channels.select(self.groups, spec)), 3)

    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            channels.parse_spec("2:9-1")


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "history.json")
        self.history = CaptureHistory(self.path)
        ch2 = os.path.join(os.path.dirname(self.path), "BonDriver_Test.ch2")
        with open(ch2, "w", encoding="utf-8") as file:
            file.write(CH2)
        self.groups = channels.load_groups(ch2)

    def test_untouched_first(self):
        now = datetime.now()
        self.history.mark_captured("d", [self.groups[0]], now)
        ordered = self.history.order("d", self.groups)
        self.assertEqual(ordered[-1].key, self.groups[0].key)

    def test_oldest_first(self):
        now = datetime.now()
        self.history.mark_captured("d", [self.groups[0]], now - timedelta(hours=5))
        self.history.mark_captured("d", [self.groups[1]], now - timedelta(hours=1))
        self.history.mark_captured("d", [self.groups[2]], now - timedelta(hours=9))
        ordered = self.history.order("d", self.groups)
        self.assertEqual([g.key for g in ordered],
                         [self.groups[2].key, self.groups[0].key, self.groups[1].key])

    def test_addon_freshness_counts(self):
        now = datetime.now()
        # ローカルには履歴が無くても、他所で更新済みなら後回しになる
        freshness = {self.groups[0].key: now}
        ordered = self.history.order("d", self.groups, freshness)
        self.assertEqual(ordered[-1].key, self.groups[0].key)

    def test_report_records_seconds_and_completion(self):
        when = datetime.now()
        self.history.apply_report("d", [
            {"key": "0:4", "time": when, "complete": True, "seconds": 61},
            {"key": "2:15", "time": when, "complete": False, "seconds": 360},
        ])
        self.assertEqual(self.history.estimate_seconds("d", self.groups[0]), 61)
        self.assertGreater(self.history.captured_at("d", self.groups[0]), datetime.min)
        # 完了しなかったチャンネルは「巡回した」だけで取得済みにはしない
        self.assertEqual(self.history.captured_at("d", self.groups[1]), datetime.min)

    def test_survives_a_reload(self):
        when = datetime.now()
        self.history.mark_captured("d", [self.groups[0]], when)
        again = CaptureHistory(self.path)
        self.assertGreater(again.captured_at("d", self.groups[0]), datetime.min)

    def test_estimate_falls_back_to_the_median(self):
        when = datetime.now()
        self.history.apply_report("d", [
            {"key": "0:4", "time": when, "complete": True, "seconds": 100},
            {"key": "2:15", "time": when, "complete": True, "seconds": 200},
        ])
        # 実測の無いチャンネルは、そのドライバの中央値を使う
        self.assertEqual(self.history.estimate_seconds("d", self.groups[2]), 200)


class FreeWindowTest(unittest.TestCase):
    def reservation(self, start, minutes, tuner="0"):
        return Reservation(
            id="1", title="番組", start=start,
            end=start + timedelta(minutes=minutes),
            tuner_id=tuner, tuner_name="D.dll")

    def test_open_when_nothing_is_booked(self):
        window, blocker = free_until([], 1, "D.dll", datetime.now())
        self.assertGreater(window, 0)
        self.assertIsNone(blocker)

    def test_stops_before_a_recording(self):
        now = datetime.now()
        window, blocker = free_until(
            [self.reservation(now + timedelta(minutes=30), 30)],
            1, "D.dll", now)
        self.assertEqual(window, 30 * 60)
        self.assertIsNotNone(blocker)

    def test_guard_is_subtracted(self):
        now = datetime.now()
        window, _ = free_until(
            [self.reservation(now + timedelta(minutes=30), 30)],
            1, "D.dll", now, guard=600)
        self.assertEqual(window, 20 * 60)

    def test_a_spare_tuner_stays_free(self):
        now = datetime.now()
        booked = [self.reservation(now + timedelta(minutes=30), 30)]
        # 2本あって1本だけ埋まるなら、1本は空いたまま
        self.assertGreater(free_until(booked, 2, "D.dll", now)[0], 60 * 60)
        # 2本必要なら、その予約で足りなくなる
        self.assertEqual(free_until(booked, 2, "D.dll", now, needed=2)[0], 30 * 60)

    def test_more_needed_than_exist(self):
        self.assertEqual(free_until([], 1, "D.dll", datetime.now(), needed=2)[0], 0)


class ScheduleTest(unittest.TestCase):
    def config(self, **kwargs):
        return config_module.Config(exe="x", **kwargs)

    def test_daily_times(self):
        config = self.config(times=[clock_time(4, 30), clock_time(16, 30)])
        moment = datetime(2026, 9, 2, 10, 0)
        self.assertEqual(next_run_after(config, moment), datetime(2026, 9, 2, 16, 30))

    def test_wraps_to_tomorrow(self):
        config = self.config(times=[clock_time(4, 30)])
        moment = datetime(2026, 9, 2, 10, 0)
        self.assertEqual(next_run_after(config, moment), datetime(2026, 9, 3, 4, 30))

    def test_interval(self):
        config = self.config(every=3600)
        moment = datetime(2026, 9, 2, 10, 0)
        self.assertEqual(next_run_after(config, moment), datetime(2026, 9, 2, 11, 0))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.exe = os.path.join(self.directory, "TVTest.exe")
        with open(self.exe, "wb") as file:
            file.write(b"not really TVTest")
        self.path = os.path.join(self.directory, "config.toml")

    def values(self, **overrides):
        values = {
            "exe": self.exe,
            "extra_args": ["/log"],
            "drivers": [{
                "name": "BonDriver_Test.dll", "timeout": "40m",
                "min_window": "15m", "instances": 2, "idle": "90s",
                "channels": "2:*", "enabled": True,
            }],
            "times": ["04:30"],
            "every": "",
            "run_at_start": False,
            "priority": {"enabled": True, "use_addon": True,
                         "reserve": "2m", "min_age": "6h"},
            "edcb": {"url": "http://127.0.0.1:5510", "guard": "10m",
                     "poll": "30s", "timeout": "10s",
                     "default_start_margin": "30s", "default_end_margin": "30s",
                     "required": True},
            "server": {"enabled": True, "api_port": 8077, "ui_port": 8099,
                       "token": "t", "data_dir": "", "retention_days": 14},
            "addon": {"url": "http://example", "token": "", "name": "runner",
                      "timeout": "10s"},
            "log_file": "",
            "log_level": "INFO",
        }
        values.update(overrides)
        return values

    def test_round_trip(self):
        saved = config_module.save(self.values(), self.path)
        self.assertEqual(saved.drivers[0].timeout, 2400)
        self.assertEqual(saved.drivers[0].instances, 2)
        self.assertEqual(saved.drivers[0].idle, 90)
        self.assertEqual(saved.drivers[0].channels, "2:*")
        self.assertEqual(saved.priority.min_age, 6 * 3600)
        self.assertTrue(saved.server.enabled)
        self.assertEqual(saved.times[0].hour, 4)

        # 読み書きを繰り返しても値が変わらない
        again = config_module.save(config_module.values_from(saved), self.path)
        self.assertEqual(config_module.values_from(again),
                         config_module.values_from(saved))

    def test_windows_paths_survive(self):
        # パスの円記号がエスケープとして解釈されないこと
        odd = os.path.join(self.directory, "sub dir")
        os.makedirs(odd, exist_ok=True)
        exe = os.path.join(odd, "TVTest.exe")
        with open(exe, "wb") as file:
            file.write(b"x")
        saved = config_module.save(self.values(exe=exe), self.path)
        self.assertEqual(saved.exe, exe)

    def test_a_bad_edit_never_lands(self):
        config_module.save(self.values(), self.path)
        good = open(self.path, encoding="utf-8").read()
        with self.assertRaises(config_module.ConfigError):
            config_module.save(self.values(times=[], every=""), self.path)
        # 保存に失敗しても、元の設定はそのまま残っている
        self.assertEqual(open(self.path, encoding="utf-8").read(), good)


if __name__ == "__main__":
    unittest.main(verbosity=2)
