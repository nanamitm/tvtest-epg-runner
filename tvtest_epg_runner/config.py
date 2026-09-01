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


# -- 設定の書き出し --------------------------------------------------------
#
# tomllib は読み込み専用なので、値を差し込んだテンプレートを書き出す。設定
# ファイルを手で開いたときに説明が残るように、コメントごと組み立てる。

TEMPLATE = """# TVTest EPG Runner の設定
# 設定画面から保存すると、このファイルは書き換えられます。

[tvtest]
# 番組表取得専用の TVTest。視聴用とは別のフォルダを使ってください。
exe = {exe}
extra_args = {extra_args}

{drivers}
[schedule]
{schedule}
# 起動直後にも1回実行する場合は true。
run_at_start = {run_at_start}

[edcb]
# EpgTimerSrv の HTTP サーバ (EnableHttpSrv=1 / HttpPort)。
url = {edcb_url}
# 予約のこれだけ手前で取得をやめます。EDCB の NGEpgCapTime と同じ考え方です。
guard = {edcb_guard}
# 実行中に予約を確認する間隔。
poll = {edcb_poll}
timeout = {edcb_timeout}
# 予約に個別マージンがないときに使う既定値。
default_start_margin = {edcb_start_margin}
default_end_margin = {edcb_end_margin}
# true にすると、EDCB に問い合わせできないときは取得しません。
required = {edcb_required}

[addon]
# Home Assistant の TVTest EPG Sync アドオン。空にすると通知しません。
url = {addon_url}
token = {addon_token}
name = {addon_name}
timeout = {addon_timeout}

[log]
{log_file}level = {log_level}
"""

DRIVER_TEMPLATE = """[[driver]]
name = {name}
# 取得の制限時間。EDCB の予約が近ければ自動で短くなります。
timeout = {timeout}
# 予約までこれ未満しか空きがなければスキップします。
min_window = {min_window}
enabled = {enabled}
"""


def duration_text(seconds):
    """Render seconds the way the configuration file writes them."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    parts = []
    for unit, size in (("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{unit}")
    return "".join(parts)


def values_from(config):
    """The editable values of a loaded configuration, as the dialog wants them."""
    return {
        "exe": config.exe,
        "extra_args": list(config.extra_args),
        "drivers": [
            {
                "name": driver.name,
                "timeout": duration_text(driver.timeout),
                "min_window": duration_text(driver.min_window),
                "enabled": driver.enabled,
            }
            for driver in config.drivers
        ],
        "times": [f"{entry:%H:%M}" for entry in config.times],
        "every": duration_text(config.every) if config.every else "",
        "run_at_start": config.run_at_start,
        "edcb": {
            "url": config.edcb.url,
            "guard": duration_text(config.edcb.guard),
            "poll": duration_text(config.edcb.poll),
            "timeout": duration_text(config.edcb.timeout),
            "default_start_margin": duration_text(config.edcb.default_start_margin),
            "default_end_margin": duration_text(config.edcb.default_end_margin),
            "required": config.edcb.required,
        },
        "addon": {
            "url": config.addon.url,
            "token": config.addon.token,
            "name": config.addon.name,
            "timeout": duration_text(config.addon.timeout),
        },
        "log_file": "" if _is_default_log(config) else config.log_file,
        "log_level": config.log_level,
    }


def _is_default_log(config):
    default = os.path.join(
        os.path.dirname(os.path.abspath(config.path)), "runner.log")
    return os.path.normcase(config.log_file) == os.path.normcase(default)


def render(values):
    drivers = "".join(
        DRIVER_TEMPLATE.format(
            name=_string(driver["name"]),
            timeout=_string(driver["timeout"]),
            min_window=_string(driver["min_window"]),
            enabled=_bool(driver["enabled"]),
        ) + "\n"
        for driver in values["drivers"]
    )

    if values.get("every"):
        schedule = (
            "# 前回の実行からこの間隔で繰り返します。\n"
            f"every = {_string(values['every'])}\n"
        )
    else:
        times = ", ".join(_string(entry) for entry in values["times"])
        schedule = f"# 毎日この時刻に実行します。\ntimes = [{times}]\n"

    log_file = ""
    if values.get("log_file"):
        log_file = f"file = {_string(values['log_file'])}\n"

    edcb = values["edcb"]
    addon = values["addon"]
    return TEMPLATE.format(
        exe=_string(values["exe"]),
        extra_args="[" + ", ".join(_string(a) for a in values["extra_args"]) + "]",
        drivers=drivers,
        schedule=schedule,
        run_at_start=_bool(values["run_at_start"]),
        edcb_url=_string(edcb["url"]),
        edcb_guard=_string(edcb["guard"]),
        edcb_poll=_string(edcb["poll"]),
        edcb_timeout=_string(edcb["timeout"]),
        edcb_start_margin=_string(edcb["default_start_margin"]),
        edcb_end_margin=_string(edcb["default_end_margin"]),
        edcb_required=_bool(edcb["required"]),
        addon_url=_string(addon["url"]),
        addon_token=_string(addon["token"]),
        addon_name=_string(addon["name"]),
        addon_timeout=_string(addon["timeout"]),
        log_file=log_file,
        log_level=_string(values["log_level"]),
    )


def save(values, path):
    """Write the values and load them back, so a bad edit never lands.

    Returns the freshly loaded configuration.
    """
    text = render(values)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    check_path = path + ".check"
    with open(check_path, "w", encoding="utf-8", newline="\r\n") as file:
        file.write(text)
    try:
        load(check_path)
    finally:
        try:
            os.remove(check_path)
        except OSError:
            pass

    temporary = path + ".new"
    with open(temporary, "w", encoding="utf-8", newline="\r\n") as file:
        file.write(text)
    os.replace(temporary, path)
    return load(path)


def _string(value):
    text = "" if value is None else str(value)
    if "'" in text or "\n" in text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\n")
        return f'"{escaped}"'
    # Windows のパスをそのまま書けるようリテラル文字列を使う
    return f"'{text}'"


def _bool(value):
    return "true" if value else "false"
