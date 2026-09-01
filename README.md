# TVTest EPG Runner

Keeps the [TVTest EPG Sync](../home-assistant-addons/tvtest_epg_sync) add-on fed
with fresh program data: it runs TVTest's command line EPG capture on a
schedule, works around EDCB's recordings, and reports what happened.

It lives in the tray, so a round can also be started or stopped by hand.

## What it does

- Runs `TVTest.exe /d <BonDriver> /epgcaptureexit /epgcapturetimeout <限界>`
  for each configured tuner, one after another.
- Asks EpgTimerSrv how long that BonDriver stays free
  (`/api/EnumTunerReserveInfo`, which groups reservations by tuner) and
  **shortens the capture to fit the gap** before the next recording, margins
  and a configurable guard included. A driver with several tuner instances can
  still be used while one of them records.
- Skips a driver whose gap is shorter than `min_window`.
- Keeps polling while the capture runs, and **aborts it** through TVTest's
  `TVTest_EpgCaptureCancel_<pid>` event as soon as a recording comes into
  range. TVTest then flushes the EPG it has already collected to the sync
  server before exiting, so an aborted round is not a wasted one.
- Posts the result of every round to the add-on.
- Picks a capture back up after being killed: while TVTest runs, the runner
  notes it in `capture-state.json`, and on the next start it attaches to that
  process again — same watchdog, same reporting — instead of leaving it to hold
  a tuner unwatched. The note is only trusted when the pid is still alive and
  still running the configured `TVTest.exe`.

Requires a TVTest build with `/epgcapturetimeout` and the cancel event, i.e.
[nanamitm/TVTest](https://github.com/nanamitm/TVTest) `develop` at
`Add ways to cancel command-line EPG capture` or later.

## Install

```
pip install -r requirements.txt
copy config.example.toml %APPDATA%\TVTestEpgRunner\config.toml
notepad %APPDATA%\TVTestEpgRunner\config.toml
```

Point `exe` at a TVTest folder **dedicated to capturing** — not the one used
for watching, whose settings and tuner it would otherwise fight over.

Start it with `run.cmd` (no console window), or `python -m tvtest_epg_runner`.
To have it come up with Windows, put a shortcut to `run.cmd` in
`shell:startup`.

## Usage

```
python -m tvtest_epg_runner              # tray application
python -m tvtest_epg_runner --check      # settings, schedule and free windows
python -m tvtest_epg_runner --once       # one round, on the console
python -m tvtest_epg_runner --once BonDriver_dantto4k.dll
```

`--once` exits with 0 when every capture completed, 2 when something was
skipped, 3 when a capture ended incomplete, and 1 on a configuration error.

The tray menu offers the next run time, `今すぐ取得`, a per-driver submenu,
`取得を中止`, the last round's results, and shortcuts to the log and the
configuration file.

## Configuration

See [config.example.toml](config.example.toml); every key is commented. The
important ones:

| Key | Meaning |
|---|---|
| `driver.timeout` | Upper bound for one capture. It is shortened when EDCB needs the tuner sooner. |
| `driver.min_window` | Skip this driver when less free time than this is left. |
| `edcb.guard` | Stop this long before a recording starts (EDCB's own `NGEpgCapTime` is the same idea). |
| `edcb.required` | With `true`, no capture runs while EpgTimerSrv cannot be reached. |
| `schedule.times` / `schedule.every` | Daily times, or a fixed interval. |

A driver EDCB does not know about is never held back — it simply runs with its
configured time limit.

## Notes

- The cancel event lives in the session local namespace, so the runner has to
  be logged into the same session as the TVTest it starts. This is why it is a
  tray application rather than a service.
- EDCB's own EPG capture (`EpgCapTime`) is not visible through the reservation
  API. If both are configured, keep their times apart.
- Only one instance runs at a time; a second one exits immediately.
