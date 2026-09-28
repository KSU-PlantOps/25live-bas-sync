[Documentation](README.md) › The command line

# The command line, and running without Docker

Every sync — from the service's schedule, from *Sync now*, or from cron — is
the same `bas-sync` command. In Docker, run it with
`docker compose run --rm sync sync <args>`; from a checkout, `python main.py`.

- [Commands](#commands)
- [Exit codes](#exit-codes)
- [Install without Docker](#install-without-docker)
- [Scheduling without Docker](#scheduling-without-docker)
- [Logs](#logs)
- [The desktop editor](#the-desktop-editor)

## Commands

```bash
python main.py --list-drivers   # what BAS integrations are available
python main.py --validate       # pre-flight: config, auth, bookings, targets
python main.py --test-alert     # prove the reports/alerts actually reach you
python main.py --discover       # list 25Live spaces with upcoming events
python main.py --dry-run        # fetch + build, print what WOULD be written
python main.py                  # live run
python main.py --system ebo_campus   # limit to one BAS (commissioning)
python main.py --force          # override the mass-clear safety check
```

- **`--validate`** is the deployment-confidence command, and writes nothing. It
  checks that the room map loads and 25Live authenticates. It checks that
  25Live **actually returns bookings** for your rooms — and if it returns none,
  tries each `state_param_style` and names any that work. It checks that every
  BAS is reachable and **every schedule target resolves**, reporting each
  BACnet schedule's value type and any exceptions a live run would replace.
  Finally it checks that the safety state can be kept. Run it first.
- **`--dry-run`** contacts no BAS at all, shows each driver's *actual*
  encoding — for BACnet, the per-date special events that would go on the
  wire — and says whether the safety check would let a live run through.
- **`--discover`** (optionally `--discover-days N`, default 30) prints spaces
  with bookings as ready-to-paste YAML for `space_mapping.yaml`. It also keeps
  what it found — each space's name, capacity, building where 25Live gives one,
  and number of bookings — in `state/discovery.json`, which the web UI's
  [setup guide](web-ui.md#the-setup-guide) offers for import.
- **`--test-alert`** sends a test through every configured channel — email
  gets a sample run report — and reports each one. Worth running the day you
  set alerting up: alerting only matters when something has already gone
  wrong, which is a bad time to discover the relay rejects your `from` address.
- **`--system NAME`** limits a run to one BAS system, for commissioning one
  before the rest.
- **`--force`** overrides the [mass-clear safety check](safety.md) — the
  right answer at semester break, when the drop is real.

`--config`, `--defaults` and `--space-map` point at the settings files, and
`--verbose` logs more. `--help` lists everything.

## Exit codes

For monitoring:

| Code | Meaning |
|---|---|
| `0` | OK |
| `1` | Unhandled error or bad configuration |
| `2` | Room map problem: the map is unusable, or broken rows were left out and the rest synced |
| `3` | BAS unreachable |
| `4` | 25Live fetch failed |
| `5` | Write failures |
| `6` | Validation failed |
| `7` | Safety abort |
| `8` | Another sync was already running |
| `130` | Interrupted |

## Install without Docker

**Requirements:**

- **Python 3.13 or newer — 3.14 recommended.** Older versions are past end of
  life and no longer receive security fixes, which matters for a process
  holding service credentials on a controls network; the sync refuses to start
  on them.
- A **local 25Live account** (not SSO) with read access and Series25
  WebServices enabled — see [25Live setup](25live.md).
- Whatever your BAS side needs — see [BAS setup](bas-setup.md).

For a one-shot nightly job from Windows Task Scheduler or cron, with the
desktop editor. Use a virtual environment, and point the scheduled task at
*its* Python — then "the dependencies are installed for a different Python
than the job runs" can't happen:

```bash
git clone https://github.com/KSU-PlantOps/25live-bas-sync.git
cd 25live-bas-sync
python3.14 -m venv .venv                      # Windows: py -3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt
.venv/bin/pip install -r requirements-bacnet.txt -c constraints.txt   # BACnet driver
```

`constraints.txt` pins every dependency to the versions CI tested. The BACnet
line pulls in [BACpypes3](https://github.com/JoelBender/BACpypes3); it's
imported lazily, so a REST-only site can skip it.

Alternatively `pip install ".[bacnet]"` installs the package with two commands,
`bas-sync` (the sync) and `bas-sync-editor` (the desktop editor); add `web`
(`".[bacnet,web]"`) for `bas-sync-service`, the [web UI](web-ui.md). Each
[release](https://github.com/KSU-PlantOps/25live-bas-sync/releases) attaches
the wheel too, so a machine without git can run
`pip install "./25live_bas_sync-X.Y.Z-py3-none-any.whl[bacnet]"`. Installed
either way, the config files are looked for in the current directory, or in
`$BAS_HOME`.

Then create the settings files — see [Configuration](configuration.md).

CI tests 3.13 and 3.14, each with and without BACpypes3.

> [!NOTE]
> **Windows:** `requirements.txt` includes `tzdata` on purpose — Windows has no
> system timezone database, so without it `ZoneInfo(...)` raises
> `ZoneInfoNotFoundError` and the sync won't start.

## Scheduling without Docker

Run it once per night, from anywhere that can reach both 25Live and your BAS.
Only one live sync runs at a time: a second one exits `8` without touching
anything.

**Windows Task Scheduler:** Program `<install-dir>\.venv\Scripts\python.exe`,
Arguments `main.py`, Start in `<install-dir>`, trigger Daily at e.g. 02:00.
Choose *Run whether user is logged on or not*, and set the `BAS_*` variables
for the account that runs the task.

**Linux/macOS cron:**

```
0 2 * * *  cd /opt/25live-bas-sync && .venv/bin/python main.py
```

## Logs

Logs default to `logs/25live_sync.log` (override with `log_file`), rotating at
`log_max_mb` (default 10 MB) and keeping `log_backups` (default 10) old files.
If that directory isn't writable the sync logs to stdout instead.

## The desktop editor

With Docker, the [web UI](web-ui.md) does all of this in the browser. Without
it, run `python editor.py` (Windows users can double-click `Edit-Rooms.bat`) —
both use the same checks:

- **Rooms** tab — Add/Edit/Delete rooms. **Building**, **Floor** and **System**
  are dropdowns, so joining a roll-up or moving a room to another BAS is a pick
  from a list rather than something to remember. The Target is checked against
  the chosen system's driver when you confirm the row.
- **Buildings** tab — manage roll-up schedules and each building's campus;
  renaming a building id repoints the rooms and floors that referenced it.
  Deleting one removes its floors too, and won't proceed while rooms roll up
  *only* into it.
- **Floors** tab — per-floor corridor schedules (building + floor # + target).
- **Connection** tab — edit `config.yaml` in the editor: the 25Live
  instance/account, your BAS systems, and the timezone. Pick a system from the
  dropdown and the fields follow its driver, so a BACnet system shows a NIC
  address and BBMD while a Niagara one shows host/port/ORDs. **Add…** creates a
  new system. Passwords are never stored here — they stay in the `BAS_*`
  environment variables — and sections you don't see (`retry`, `alerts`,
  `safety`, `monitoring`) are preserved on save.
- **Defaults** tab — the global run-up/run-down/merge-gap/lookahead values
  (saved to `defaults.yaml`).
- **Tools** menu — *Test 25Live connection*, *Test BAS connections*
  (health-checks every configured system and reports them all, so "BACnet is
  fine, the EBO API is down" is the answer you get), and *Preview (dry run)*.
  They run in the background, so the window stays responsive.

Every table has live search, click-to-sort headers, and a Duplicate action, and
the whole window **follows your OS light/dark setting** automatically. One Save
(Ctrl+S) handles the room map, connection settings, and defaults together.

**Save runs the same checks the nightly sync does** — cross-row problems
included — and asks before saving anything the sync would reject. It writes
only the files that actually changed, atomically, keeping a `.bak` of the
previous version, and it won't overwrite a `config.yaml` it couldn't read.

> [!NOTE]
> Rewriting a file drops its `#` comments. `config.yaml` is only rewritten
> when you change a connection setting, so a hand-commented config survives
> room edits. For a per-row note in the room map, use the **Note** field
> (stored as a `note:` key the sync ignores).
