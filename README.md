# TVTest EPG Runner

Keeps the [TVTest EPG Sync](../home-assistant-addons/tvtest_epg_sync) add-on fed
with fresh program data: it runs TVTest's command line EPG capture on a
schedule, works around EDCB's recordings, and reports what happened.

It lives in the tray (Qt 6 / PySide6), so a round can also be started or
stopped by hand, and everything it does is configured from a settings dialog.

## What it does

- Runs `TVTest.exe /d <BonDriver> /epgcaptureexit /epgcapturetimeout <限界>`
  for each configured tuner, several at a time where the tuners allow it.
- Captures the **least recently captured channels first**, as many as the free
  time allows, and deals them across the tuners a driver can spare
  (`instances`). A round that cannot cover everything picks up where it left
  off next time, so one tuner in short slices covers the same ground as
  several in parallel — just slower.
- Counts a channel as fresh when anyone else on the LAN refreshed it, by
  reading the add-on's per-service update times.
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

Channel selection needs `/epgcapturech` and `/epgcapturereport`; the report is
how the runner learns which channels a capture actually finished, and how long
each one took. Requires a TVTest build with those, the cancel event and
`/epgcapturetimeout`, i.e.
[nanamitm/TVTest](https://github.com/nanamitm/TVTest) `develop` at
`Add ways to cancel command-line EPG capture` or later.

## Install

```
pip install -r requirements.txt   # PySide6 と requests
copy config.example.toml %APPDATA%\TVTestEpgRunner\config.toml
notepad %APPDATA%\TVTestEpgRunner\config.toml
```

Point `exe` at a TVTest folder **dedicated to capturing** — not the one used
for watching, whose settings and tuner it would otherwise fight over.

Start it with `run.cmd` (no console window), or `python -m tvtest_epg_runner`.
Tick **Windows にログオンしたときに起動する** in the settings to have it come
up on its own: that writes a logon entry running `run.pyw`, which starts the
tray application from anywhere without a console.

It is a logon entry rather than a service on purpose. The tray icon needs a
desktop, and the event that cancels a capture lives in the session namespace,
so the runner has to share a session with the TVTest it starts.

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
`取得を中止`, the last round's results, `設定…` and the log. Double clicking the
icon opens the settings too.

## Settings

`設定…` edits everything the configuration file holds, in five tabs: 全般
(TVTest, extra arguments, logging), チューナー (the drivers to visit, in order,
picked from the `BonDriver_*.dll` files next to TVTest), スケジュール (daily
times or an interval), EDCB and 通知. The last two each carry a **接続を確認**
button that reports what the runner would see: the free window per tuner for
EDCB, and whether the add-on is new enough to accept capture reports.

Saving writes the file and loads it straight back through the same validation
the runner uses at startup, so a bad edit is reported in the dialog instead of
landing on disk. The settings take effect immediately — no restart, and a
capture in progress is left alone.

## Configuration

See [config.example.toml](config.example.toml); every key is commented. The
important ones:

| Key | Meaning |
|---|---|
| `driver.timeout` | Upper bound for one capture. It is shortened when EDCB needs the tuner sooner. |
| `driver.min_window` | Skip this driver when less free time than this is left. |
| `driver.instances` | How many captures to run at once on this driver. Lowered to what EDCB leaves free. |
| `driver.channels` | Limit the pool to part of the channel list (`/epgcapturech` syntax). |
| `priority.enabled` | Capture the stalest channels first instead of walking the whole list. |
| `priority.use_addon` | Let the add-on's update times count as a capture. |

Starting at logon is not part of the configuration file — it is a registry
entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, written
and removed by the checkbox.
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
