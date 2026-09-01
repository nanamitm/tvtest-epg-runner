"""Load and validate the runner's TOML configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time as clock_time

from .util import parse_duration


class ConfigError(Exception):
    pass


@dataclass
class DriverConfig:
    name: str
    timeout: int = 40 * 60
    min_window: int = 10 * 60
    enabled: bool = True


@dataclass
class EdcbConfig:
    url: str = "http://127.0.0.1:5510"
    guard: int = 10 * 60
    poll: int = 30
    timeout: int = 10
    default_start_margin: int = 30
    default_end_margin: int = 30
    required: bool = True


@dataclass
class AddonConfig:
    url: str = ""
    token: str = ""
    name: str = "epg-runner"
    timeout: int = 10


@dataclass
class Config:
    exe: str
    extra_args: list = field(default_factory=lambda: ["/log"])
    drivers: list = field(default_factory=list)
    times: list = field(default_factory=list)
    every: int | None = None
    run_at_start: bool = False
    edcb: EdcbConfig = field(default_factory=EdcbConfig)
    addon: AddonConfig = field(default_factory=AddonConfig)
    log_file: str = ""
    log_level: str = "INFO"
    state_file: str = ""
    path: str = ""

    @property
    def enabled_drivers(self):
        return [driver for driver in self.drivers if driver.enabled]


def default_config_path():
    override = os.environ.get("TVTEST_EPG_RUNNER_CONFIG")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "TVTestEpgRunner", "config.toml")


def load(path=None):
    path = path or default_config_path()
    if not os.path.isfile(path):
        raise ConfigError(f"設定ファイルがありません: {path}")

    with open(path, "rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"設定ファイルを読めません: {error}") from error

    tvtest = data.get("tvtest", {})
    exe = tvtest.get("exe", "")
    if not exe:
        raise ConfigError("[tvtest] の exe に TVTest.exe のパスを指定してください。")
    if not os.path.isfile(exe):
        raise ConfigError(f"TVTest.exe が見つかりません: {exe}")

    drivers = []
    for entry in data.get("driver", []):
        name = entry.get("name")
        if not name:
            raise ConfigError("[[driver]] には name が必要です。")
        drivers.append(DriverConfig(
            name=name,
            timeout=parse_duration(entry.get("timeout"), 40 * 60),
            min_window=parse_duration(entry.get("min_window"), 10 * 60),
            enabled=bool(entry.get("enabled", True)),
        ))
    if not drivers:
        raise ConfigError("[[driver]] を少なくとも1つ指定してください。")

    schedule = data.get("schedule", {})
    times = [_parse_clock(value) for value in schedule.get("times", [])]
    every = parse_duration(schedule.get("every"), None)
    if not times and not every:
        raise ConfigError("[schedule] に times か every のどちらかを指定してください。")
    if times and every:
        raise ConfigError("[schedule] の times と every は同時に指定できません。")

    edcb_data = data.get("edcb", {})
    edcb = EdcbConfig(
        url=edcb_data.get("url", EdcbConfig.url),
        guard=parse_duration(edcb_data.get("guard"), EdcbConfig.guard),
        poll=parse_duration(edcb_data.get("poll"), EdcbConfig.poll),
        timeout=parse_duration(edcb_data.get("timeout"), EdcbConfig.timeout),
        default_start_margin=parse_duration(
            edcb_data.get("default_start_margin"), EdcbConfig.default_start_margin),
        default_end_margin=parse_duration(
            edcb_data.get("default_end_margin"), EdcbConfig.default_end_margin),
        required=bool(edcb_data.get("required", True)),
    )

    addon_data = data.get("addon", {})
    addon = AddonConfig(
        url=addon_data.get("url", ""),
        token=addon_data.get("token", ""),
        name=addon_data.get("name", AddonConfig.name),
        timeout=parse_duration(addon_data.get("timeout"), AddonConfig.timeout),
    )

    log_data = data.get("log", {})
    log_file = log_data.get("file") or os.path.join(
        os.path.dirname(os.path.abspath(path)), "runner.log")

    return Config(
        exe=exe,
        extra_args=list(tvtest.get("extra_args", ["/log"])),
        drivers=drivers,
        times=times,
        every=every,
        run_at_start=bool(schedule.get("run_at_start", False)),
        edcb=edcb,
        addon=addon,
        log_file=log_file,
        log_level=str(log_data.get("level", "INFO")).upper(),
        state_file=os.path.join(
            os.path.dirname(os.path.abspath(log_file)), "capture-state.json"),
        path=os.path.abspath(path),
    )


def _parse_clock(value):
    text = str(value).strip()
    try:
        hour, minute = text.split(":")
        return clock_time(int(hour), int(minute))
    except ValueError as error:
        raise ConfigError(f"時刻の指定が不正です: {value!r} (HH:MM 形式)") from error
