# 25Live → BAS Schedule Sync

Drive your building automation occupancy from your room bookings. This tool
pulls confirmed events from **CollegeNET 25Live** (Series25 WebServices) and
writes them into your building automation system as schedule exceptions, so HVAC
and lighting pre-condition for booked rooms and stand down when they're empty.

It speaks **standard BACnet**, so it is not tied to any one vendor: **Automated
Logic WebCTRL**, **Schneider EcoStruxure Building Operation**, **Tridium
Niagara**, and any other BTL-listed controller all expose the same Schedule
objects. One nightly run drives all of them at once on a mixed campus — and
schedules each building as finely as that building actually supports.

It runs as a **Docker container with a web UI**: status and history, "Sync
now", and editing the room map and settings in the browser, with the sync
running on its own schedule. It can also run as a plain nightly command from
cron or Task Scheduler. It is read-only against 25Live, only ever writes
occupancy schedules, and can email you a report of exactly what it scheduled.

## Contents

- [Why BACnet](#why-bacnet)
- [How finely can you schedule?](#how-finely-can-you-schedule)
- [Features](#features)
- [How it works](#how-it-works)
- [Quick start (Docker)](#quick-start-docker)
- [The web UI](#the-web-ui)
- [Run with Docker](#run-with-docker)
- [Install without Docker](#install-without-docker)
- [Configure](#configure)
- [Running](#running)
- [Reports and alerts](#reports-and-alerts)
- [Safety rails](#safety-rails)
- [BAS setup, by vendor](#bas-setup-by-vendor)
- [25Live setup](#25live-setup)
- [Upgrading](#upgrading)
- [Tests](#tests)

## Why BACnet

Every BTL-listed system exposes standard **Schedule objects** (ASHRAE 135
Object_Type 17) — that is what the listing requires. So rather than chase a
separate API per vendor across their version histories, the default driver
writes the one thing all of them already understand: the `Exception_Schedule`
property.

| | BACnet | Vendor REST/SOAP |
|---|---|---|
| Contract | A published standard, stable for decades | Changes across versions and add-on packs |
| Coverage | Every BTL-listed system, one code path | One integration per vendor |
| Auth | None — the network *is* the security boundary | Real accounts and TLS |
| Visibility | Exceptions show up in the vendor tool | Native objects |

The tradeoff that matters is the third row: BACnet/IP has no authentication, so
this must run on a segmented controls network (and BACnet/SC is worth asking
your vendors about for new work). Where that isn't acceptable the REST drivers
are there.

### The sync owns what it writes

**Point each target at a schedule dedicated to bookings.** Every run replaces
that schedule's whole `Exception_Schedule` in one write — that is what makes a
run atomic and a cancelled booking actually disappear. It also means anything
else in that list, such as a holiday an operator typed in, is replaced too.

So the pattern is:

1. For each zone you drive, a **booking schedule** whose weekly schedule is
   empty or Unoccupied. The sync owns its exceptions.
2. The zone's **normal schedule** keeps the building's weekly profile, its
   holidays and shutdowns. The sync never touches it.
3. The controller combines them — typically *occupied = normal OR booking*.

`--validate` reads each target's current exceptions and warns about any the
sync didn't write, before a live run erases them.

**What it writes, and what it leaves alone.** Only `Exception_Schedule`. The
weekly schedule, `Schedule_Default` and `Priority_For_Writing` are untouched.
Each value is written in the schedule's own datatype, read from its
`Schedule_Default`: binary schedules are usually ENUMERATED (active/inactive),
and controllers reject values of the wrong type.

One special event is written **per calendar date**, with time/value pairs
alternating ON at each window start and OFF at each window end:

```
2026-06-10   09:00 → ON, 11:30 → OFF, 13:00 → ON, 17:00 → OFF
```

That matters on real hardware. A naive one-entry-per-booking encoding blows past
the `Exception_Schedule` array limits field controllers actually enforce (often
10–25 entries); grouping by date caps the array at one entry per day of
lookahead no matter how heavily booked the rooms are.

## How finely can you schedule?

This is the thing to get right, and **it is not the same campus-wide.** How
granular you can be is decided by how each building was built out, not by this
tool. All three patterns are first-class, and they mix freely in one map:

| Pattern | When | How to map it |
|---|---|---|
| **Per room** | The room has its own schedulable object — a room-level VAV or FCU. Typical of **WebCTRL** sites, where scheduling is normally done per room. | Give the room a `target:`. |
| **Per floor** | The building came online with floor-level air handling, so the corridor AHU is the finest real control. | Give the room a `building:` and a `floor:`, and **no** `target:`. |
| **Per building** | The oldest wings: one air handler for the whole building. | Give the room a `building:` and nothing else. |

A room with **no `target:` is a normal, supported mapping** — it contributes its
bookings to whatever roll-ups it belongs to and writes no schedule of its own.
Occupancy always rolls up **room → floor → building**, so a booked room drives
its own schedule (if it has one), its floor's corridor (if it names a floor),
and its building's common areas.

The only thing that *is* an error is a room with neither a `target:` nor a
`building:` — its bookings would drive nothing at all, and the loader says so
rather than swallowing them.

## Features

- Pulls the next *N* days of confirmed events for a configured set of spaces,
  skipping individually cancelled occurrences of recurring events.
- **Mixed-vendor campus in one run** — every room and building names the BAS it
  lives on; rooms inherit their building's.
- **Per-room, per-floor or per-building scheduling**, mixed freely, matching
  what each building's controls actually support.
- Per-room **pre-conditioning** and **post-event** buffers, with building-wide
  and global defaults (precedence **room > building > global**).
- Merges overlapping/adjacent bookings into clean occupancy windows.
- **Building roll-up**: every room in a building automatically unions into that
  building's schedule — if *any* room is occupied, common areas run too.
- **Mass-clear safety rails** — refuses to stand the whole campus down because
  25Live had a bad day. See [Safety rails](#safety-rails).
- **Email run reports** — what was scheduled, room by room and day by day, with
  a CSV attached; on failure, and optionally every night. See
  [Reports and alerts](#reports-and-alerts).
- **Staged commissioning** — point a building at the `preview` driver and it
  logs (and CSV-exports) what it would do while the rest write for real.
- A **web UI** served by the container: status, sync history with each night's
  report, *Sync now*, *Validate*, *Dry run* and *Discover* with live output,
  and editing of rooms, buildings, floors, connections, defaults and the
  schedule — checked by the sync's own validation before anything is saved.
  See [The web UI](#the-web-ui).
- A **desktop editor** (`editor.py`, Tkinter) with the same editing, for sites
  that run the sync without Docker.
- `--dry-run`, `--validate`, `--discover` modes; typo-catching config
  validation; a dead-man's-switch ping; a BAS heartbeat; automatic retries;
  a one-run-at-a-time lock.

## How it works

```
25Live events ──► apply pre/post buffers ──► merge into occupancy windows
   ──► roll rooms up into building schedules ──► safety check
   ──► fan out to each BAS driver ──► heartbeat ──► report + alerts
```

The Python stays generic. Everything site-specific lives in three YAML files you
create from the provided examples.

## Quick start (Docker)

Docker is the recommended way to run it: one container runs the sync on its
schedule and serves the web UI. You need a Linux host or VM that can reach
25Live over HTTPS and your BAS over BACnet/IP (UDP 47808) — see
[Running in a datacenter or the cloud](#running-in-a-datacenter-or-the-cloud)
if it isn't on the controls network — with Docker and the compose plugin.

```bash
git clone https://github.com/KSU-PlantOps/25live-bas-sync.git
cd 25live-bas-sync
cp .env.example .env        # set BAS_WEB_PASSWORD and BAS_25LIVE_PASSWORD
mkdir -p config             # your settings will live here
docker compose up -d
```

Then browse to **`http://<host>:8080`**, sign in with `BAS_WEB_PASSWORD`, and
follow *Getting started* on the status page:

1. **Connection** — your 25Live instance and account, the timezone, and a BAS
   system (usually `bacnet`, with this host's `local_address`).
2. **Buildings**, then **Rooms** — *Discover spaces* lists the 25Live rooms
   with bookings, each with an *Add as a room* link.
3. **Validate**, then **Dry run**. Neither writes anything.
4. **Schedule** — nightly at 02:00 by default; add a midday time to pick up
   same-day bookings sooner.

Passwords stay in `.env` (the web UI shows which ones are missing); after
changing it, `docker compose up -d` restarts the container with the new
values. The settings you make in the web UI are ordinary YAML files in
`./config`, so you can back them up, diff them, or keep them in git.

To run a published release rather than build locally, delete `build: .` from
`docker-compose.yml` and set `image: ghcr.io/ksu-plantops/25live-bas-sync:1.3`
(the 1.3 line, which follows its patch releases; a release candidate has only
its exact tag, e.g. `:1.3.0rc1`).

## The web UI

| Page | What it does |
|---|---|
| **Status** | Last sync and its result, the next scheduled run, room-map problems, missing passwords, and buttons for *Sync now* (optionally one system, optionally *Force*), *Dry run*, *Validate*, *Test alert* and *Discover spaces*. |
| **History** | Every live sync with the report it emailed — every schedule and the exact windows written — and a CSV of every window. |
| **Jobs** | Every sync and tool, from the schedule or the web, with its full output, live while it runs. A running job can be stopped. |
| **Rooms · Buildings · Floors** | The room map, with search and sortable columns; add, edit, copy and delete. Renaming a building repoints its rooms and floors; deleting one takes its floors and won't leave rooms driving nothing. |
| **Connection** | 25Live, BAS systems (add, remove, change driver), timezone and default system; which passwords are set. *Validate* per system. |
| **Defaults · Schedule** | The run-up/run-down/merge-gap/lookahead defaults, and when the sync runs. |
| **Files** | The three YAML files as text, for anything the forms don't cover (alerts, email, safety limits); a zip of all three. |
| **Logs** | The sync's and the service's logs. |

Every sync and tool runs as the ordinary `bas-sync` command in a process of its
own, one at a time, so the web UI can't do anything the command line couldn't —
the run lock, the safety rail and the device checks all apply. Saving runs the
same checks the nightly sync does and asks before saving anything it would
reject; each save is atomic, keeps a `.bak`, and is refused if someone else
changed the file since you opened the form.

**Security.** It can start a sync that writes to building controllers, so:

- **One password**, `BAS_WEB_PASSWORD`. Without it the web UI stays off and only
  the schedule runs. Five wrong tries lock the address out for five minutes;
  changing the password signs everyone out. Sessions last 12 hours.
- **HTTPS**: set `BAS_WEB_TLS_CERT` and `BAS_WEB_TLS_KEY`, or put it behind a
  reverse proxy that terminates TLS and set `BAS_WEB_BEHIND_PROXY=1`. Only
  behind a proxy that does its own sign-in, `BAS_WEB_AUTH=none` turns the
  password off.
- **Where it listens**: with host networking (needed for BACnet) the web UI is
  on the host's port 8080 on every interface. Set `BAS_WEB_HOST` to one address,
  and firewall the port to the people who use it.
- Forms carry CSRF tokens; a strict Content-Security-Policy allows no inline
  script and nothing from other sites, so it also works with no internet at all.
- Every change and every job is logged with the address that made it, in the
  service log (**Logs → Service**).

Outside Docker, `pip install ".[bacnet,web]"` and run `bas-sync-service` (with
`BAS_WEB_PASSWORD` set) for the same service and web UI.

## Run with Docker

The image runs in one of two ways (see `docker-entrypoint.sh`):

- **`serve`** — the service: the sync on its schedule plus the web UI. This is
  what `docker-compose.yml` runs.
- **`sync [args]`** — one run of the command line, then exit with its code:
  `sync --validate`, `sync --dry-run`, or plain `sync` for a live run. Use it
  for host cron or a **Kubernetes CronJob**, or for a one-off next to the
  service:

  ```bash
  docker compose run --rm sync sync --validate
  docker compose run --rm sync sync --dry-run
  ```

  1.x-style flags straight after the image name (`--validate`) still work, and
  with no arguments the image does one live sync — or runs the service if
  `SYNC_AT` is set — as 1.x did.

> **BACnet needs host networking.** The BACnet driver binds a real NIC address
> and relies on broadcast, neither of which survives Docker's default bridge.
> Run it with `network_mode: host` (already set in the compose file) and set
> `local_address` to the *host's* address. On Docker Desktop for Mac/Windows,
> host networking is limited — run it on a Linux host or VM. The `rest` driver
> is ordinary HTTP and works under bridge networking: drop `network_mode` and
> publish the port (`ports: ["8080:8080"]`).

### Config folder

`./config` is mounted read-write at `/config`: the web UI saves `config.yaml`,
`defaults.yaml` and `space_mapping.yaml` there. The container starts as root
only long enough to sort out file ownership, then runs as **whoever owns that
folder on the host**, so the files stay yours and you can still edit them
there. Set `PUID`/`PGID` in `.env` to run as someone else. A folder Docker
created itself (root-owned and empty) is handed to the image's own user,
10001. If the web UI says it can't save, the folder isn't writable by the user
it runs as: `sudo chown -R "$(id -u):$(id -g)" config` fixes it. A read-only
mount (`:ro`) also works — the web UI then shows the settings but can't change
them.

### State, logs and the schedule

> **Keep the `state` volume.** It holds the previous run's baseline that the
> mass-clear safety check compares against, the run history, the job output
> and the run lock. Compose mounts a named volume for it; with `docker run` or
> a CronJob, mount a named volume or PVC at `/app/state` yourself. Without it
> every run starts with no history — the sync warns about that on every run,
> and the entrypoint warns if `/app/state` isn't writable.

- **Schedule**: `schedule:` in `config.yaml` — the web UI's Schedule page. Times
  are in `timezone:` from the config. The service handles daylight-saving
  changes: on the spring-forward date a time that doesn't exist runs as the
  clock jumps, and a repeated one in the fall-back hour runs once. `SYNC_AT`
  and `SYNC_ON_START` in the environment still override it, as in 1.x.
- **Logs**: `docker compose logs` for the service; the sync's and the
  service's log files are in the `logs` volume, and on the web UI's Logs page.
- **Health**: the compose file's health check runs
  `python -m bassync.service --health`, which checks the scheduler's heartbeat.
- **Stopping**: `docker stop` lets a sync that is already writing finish (up to
  `BAS_STOP_GRACE`, 90 s) before interrupting it; the compose file allows two
  minutes.
- **Hardening**: the compose file runs the container with a read-only root
  filesystem, all capabilities dropped except the few the entrypoint needs to
  hand the folders over, and `no-new-privileges`. The service itself runs as
  an ordinary user with no capabilities.

Passwords come from the environment only and are never baked into the image.
The container points at `/config/*.yaml` via the `BAS_CONFIG` /
`BAS_DEFAULTS` / `BAS_SPACE_MAP` env vars.

## Install without Docker

**Requirements:** **Python 3.13 or newer — 3.14 recommended.** Older versions
are past end of life and no longer receive security fixes, which matters for a
process holding service credentials on a controls network; the sync refuses to
start on them. A **local 25Live account** (not SSO) with read access and
Series25 WebServices enabled. Whatever your BAS side needs — see
[BAS setup](#bas-setup-by-vendor).

For a one-shot nightly job from Windows Task Scheduler or cron, with the
desktop editor. Use a virtual environment, and point the scheduled task at *its* Python — then
"the dependencies are installed for a different Python than the job runs" can't
happen:

```bash
git clone https://github.com/KSU-PlantOps/25live-bas-sync.git
cd 25live-bas-sync
python3.14 -m venv .venv                      # Windows: py -3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt
.venv/bin/pip install -r requirements-bacnet.txt -c constraints.txt   # BACnet driver
```

`constraints.txt` pins every dependency to the versions CI tested. The BACnet
line pulls in [BACpypes3](https://github.com/JoelBender/BACpypes3); it's
imported lazily, so a Niagara- or REST-only site can skip it.

Alternatively `pip install ".[bacnet]"` installs the package with two commands,
`bas-sync` (the sync) and `bas-sync-editor` (the GUI). Each
[release](https://github.com/KSU-PlantOps/25live-bas-sync/releases) attaches the
wheel too, so a machine without git can run
`pip install "./25live_bas_sync-X.Y.Z-py3-none-any.whl[bacnet]"`. Installed
either way, the config files are looked for in the current directory, or in
`$BAS_HOME`.

CI tests 3.13 and 3.14, each with and without BACpypes3.

> **Windows:** `requirements.txt` includes `tzdata` on purpose — Windows has no
> system timezone database, so without it `ZoneInfo(...)` raises
> `ZoneInfoNotFoundError` and the sync won't start.

## Configure

**1. Settings** — copy the example and edit it:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` holds your 25Live instance, your BAS systems, accounts, timezone,
safety limits and alerting. It's **gitignored**. Point elsewhere with
`--config PATH` or `$BAS_CONFIG`.

Every value is checked at startup. A wrong type or a misspelt timezone stops the
run with one line naming the key; a key the sync doesn't recognise (usually a
typo, like `notify_on_sucess`) is logged as a warning.

**2. Secrets** — passwords are **never** stored in files:

```bash
# Linux / macOS
export BAS_25LIVE_PASSWORD=...
export BAS_SYS_EBO_WEST_PASSWORD=...       # one per system that logs in (rest)

# Windows (PowerShell)
$env:BAS_25LIVE_PASSWORD = "..."
$env:BAS_SYS_EBO_WEST_PASSWORD = "..."
```

The variable name is `BAS_SYS_` + the system's name, uppercased with
non-alphanumerics as underscores. BACnet systems — including Niagara
stations reached through their BACnet export — need no credential. Alert
email uses `BAS_SMTP_PASSWORD`; a webhook URL can come from
`BAS_ALERT_WEBHOOK_URL` if you'd rather not keep it in the file.

**3. Scheduling defaults** — `cp defaults.example.yaml defaults.yaml`. This is
the operator-tunable file (run-up / run-down / merge-gap / lookahead), kept
separate from the IT-managed connection settings. Edit it by hand or in the
editor's **Defaults** tab.

**4. Room map** — `cp space_mapping.example.yaml space_mapping.yaml`, then edit
by hand or with the GUI.

### The room map

Three sections: `buildings:` (each roll-up schedule, defined once), `floors:`
(optional per-floor corridor schedules) and `spaces:` (the rooms). Each room
names its `building:` by id, and **every room in a building is automatically
unioned into that building's schedule** — you never repeat the building's
address on a room, so you can't forget to wire one up.

Each entry carries:

- **`system:`** — which BAS it lives on, a key from `systems:` in `config.yaml`.
  Rooms and floors inherit their building's; buildings fall back to
  `default_system`. With one system defined you can omit it everywhere.
- **`target:`** — the schedule's address *within* that system:

  | driver | target | meaning |
  |---|---|---|
  | `bacnet` | `12001:5` | device instance 12001, Schedule object instance 5 |
  | `bacnet` | `12001:5@10.4.2.30` | …with the address pinned, skipping Who-Is |
  | `bacnet` | `12001:5@2001:0x21` | …a routed MS/TP device: network 2001, MAC 0x21 |
  | `bacnet` | `2001:1` | a Niagara station's exported schedule — see [Tridium Niagara](#tridium-niagara) |
  | `niagara` *(deprecated)* | `Bldg/Rm101_Occ` | ORD relative to `schedule_base_path` |
  | `rest` | whatever your path template expects | |

  **Required on a building or floor** — those entries exist to name a schedule.
  **Optional on a room:** omit it for a building scheduled per floor or per air
  handler, and the room drives its roll-ups instead. See
  [How finely can you schedule?](#how-finely-can-you-schedule).

  A BACnet pin belongs to the *device*: pin it once and every target on that
  device uses it. Two spellings of the same schedule (`12001:5` and
  `12001:5@10.4.2.30`) are recognised as one and written once.

- **`floor:`** (rooms) — with a matching `floors:` entry, the room also drives
  that floor's corridor schedule. Occupancy rolls up **room → floor →
  building**: a corridor runs if any room off it is booked, and the building
  runs if any floor is.

`space_mapping.example.yaml` documents every field and shows all three
granularity patterns side by side.

**A broken row doesn't cost you the campus.** A row the loader can't use — a
non-numeric buffer, a malformed target, a floor of a building that no longer
exists — is reported and left out, the rest of the campus syncs, and the run
exits `2` so the alert fires. Nothing the broken row affects is touched that
night: not its own schedule, and not the floor or building schedules it rolls
up into, which keep their current schedule rather than being rewritten
without its bookings. A room whose own target is malformed still feeds its
floor and building. Set `safety.on_map_errors: abort` to write nothing until
the map is fixed instead.

### Editing rooms with the desktop editor

With Docker, the [web UI](#the-web-ui) does all of this in the browser. Without
it, run `python editor.py` (Windows users can double-click `Edit-Rooms.bat`) —
both use the same checks:

- **Rooms** tab — Add/Edit/Delete rooms. **Building**, **Floor** and **System**
  are dropdowns, so joining a roll-up or moving a room to another BAS is a pick
  from a list rather than something to remember. The Target is checked against
  the chosen system's driver when you confirm the row.
- **Buildings** tab — manage roll-up schedules; renaming a building id repoints
  the rooms and floors that referenced it. Deleting one removes its floors too,
  and won't proceed while rooms roll up *only* into it.
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

> Rewriting a file drops its `#` comments. `config.yaml` is only rewritten
> when you change a connection setting, so a hand-commented config survives
> room edits. For a per-row note in the room map, use the **Note** field
> (stored as a `note:` key the sync ignores).

## Running

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
  with bookings as ready-to-paste YAML for `space_mapping.yaml`.
- **`--test-alert`** sends a test through every configured channel — email
  gets a sample run report — and reports each one. Worth running the day you
  set alerting up: alerting only matters when something has already gone
  wrong, which is a bad time to discover the relay rejects your `from` address.

Exit codes (for monitoring): `0` ok · `1` unhandled error or bad configuration
· `2` room map problem (the map is unusable, or broken rows were left out and
the rest synced) · `3` BAS unreachable · `4` 25Live fetch failed · `5` write
failures · `6` validation failed · `7` safety abort · `8` another sync was
already running · `130` interrupted.

### Scheduling

In Docker, the service runs the sync on its schedule — set it on the web UI's
Schedule page. Without Docker, run it once per night, from anywhere that can
reach both 25Live and your BAS.
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

Logs default to `logs/25live_sync.log` (override with `log_file`), rotating at
`log_max_mb` (default 10 MB) and keeping `log_backups` (default 10) old files.
If that directory isn't writable the sync logs to stdout instead.

## Reports and alerts

Every live run builds a **report**:

- the outcome, the safety verdict, and each BAS system's status
- every warning and error it logged
- **every schedule with the exact windows written to it**, grouped by day:

```
[WRITTEN] Science Hall 1021  (webctrl:12100:5)
      Mon 10/05  07:45–10:30, 12:15–17:15
      Tue 10/06  07:45–09:30
[WRITTEN] Floor 2 of Liberal Arts  (webctrl:12200:120)
      no bookings — cleared
```

Set `alerts.enabled: true` and it goes out through either or both channels:

- **Email (SMTP)** — the full report as text and HTML, with every window
  attached as a CSV. Sent on failure; set `email.notify_on_success: true` for
  a morning email of what the buildings will do that week (`report: summary`
  leaves out the per-schedule list).
  - Transport: `starttls` (port 587, the default), `ssl` for implicit TLS/SMTPS
    (port 465), or `none` for an internal relay on a trusted network.
  - Leave `username` blank for an open relay; if you set it, the password must
    be in `$BAS_SMTP_PASSWORD`.
  - Connect, TLS, authentication and per-recipient rejection are each reported
    separately, because they need four different fixes.
- **Webhook** — a short summary. `webhook_format: slack` (the default) posts
  `{"text": ...}`. `teams` posts the Adaptive Card that Microsoft Teams
  *Workflows* expects, since the old Office 365 connectors have been retired.
  `generic` adds `ok`, `exit_code` and `subject` for your own tooling. It is
  sent on failure, and on success if `notify_on_success` (or
  `webhook_notify_on_success`) is on.

Alerting never changes a run's outcome — a dead mail relay won't turn a
successful sync into a failure.

**Catch the job not running at all.** Alerts only fire when the job runs. Set
`monitoring.ping_url` to a dead-man's-switch monitor — healthchecks.io, an
Uptime Kuma push monitor, Cronitor — and it's fetched after every successful
run, so the monitor alarms when the pings stop: an expired service password, a
disabled task, a rebuilt host. `ping_fail_url` is fetched after a failed run.
Pair it with a BAS-side heartbeat (`heartbeat_object` for BACnet,
`heartbeat_path` for Niagara) and the BAS can alarm on its own.

## Safety rails

The dangerous failure mode here is not a crash — it's a **successful-looking run
that writes empty schedules everywhere**. An expired 25Live service account, a
changed `state` query parameter, or a Series25 version bump all return HTTP 200
with zero events, and a naive sync would faithfully stand the entire campus
down. Nobody notices until Monday morning.

So each run compares itself to the last and refuses to write if:

- fewer than `safety.min_events` assignments came back from 25Live at all, or
- more than `safety.max_cleared_fraction` (default 34%) of the schedules that
  had bookings last time would be emptied now.

Both are about *change*, not absolute counts, so a genuinely quiet week still
has last week's state to compare against, and a first-ever run is allowed
through.

The comparison state lives in **`state/last_run.json`**. It is kept apart from
the logs, written atomically, and backed by the previous copy (`.prev`).
A run with no baseline says so at WARNING level every time, rather than quietly
passing. `--dry-run` shows the verdict a live run would get.

`--force` overrides it — the right answer at semester break, when the drop is
real. A blocked run exits `7` and emails the reason, including which schedules
would have been cleared.

This complements, rather than replaces, the other layers:

- `--validate` before deploying
- `--dry-run` before each change
- the `preview` driver for staged cut-over
- `verify_writes` reading BACnet writes back to confirm they took
- `verify_device` refusing to write through a stale pinned address

## BAS setup, by vendor

Whichever system you're on, this populates the *calendar*. You still wire the
schedule into your equipment once, in the vendor's own tool:

1. For each level you intend to drive — per room, per floor, per building, or a
   mix; see [How finely can you schedule?](#how-finely-can-you-schedule) —
   create a **dedicated booking schedule** for the sync.
2. Leave its weekly schedule empty or **Unoccupied** — the sync only writes the
   booking exceptions on top.
3. Combine it with the zone's normal schedule in your occupancy logic
   (room → that zone; floor → corridor AHU; building → common AHUs and lobbies),
   typically *occupied = normal OR booking*. Holidays and shutdowns stay on the
   normal schedule.

Then run `python main.py --validate`, which resolves every target, reads each
one's value type, and warns about exceptions a live run would replace — all
without writing.

### Automated Logic WebCTRL

**Use the `bacnet` driver.** ALC is BACnet-native — WebCTRL schedules *are*
BACnet Schedule objects in the controllers, so this is the supported, documented
integration path rather than a workaround.

WebCTRL sites normally schedule **per room**, so give each room its own
`target:`. Find the two numbers in WebCTRL: the controller's **device instance**
(under the module's BACnet properties) and the schedule's **object instance**.
`--validate` reads each schedule's `object-name` back, so a wrong number fails
pre-flight instead of writing somewhere unexpected. Many ALC field controllers
sit on MS/TP behind a router; Who-Is finds them, or pin them with the routed
form `12001:5@<network>:<mac>`.

> **Operational gotcha worth planning for:** WebCTRL treats its own database
> as the source of schedules. A **download** to a controller — and, depending
> on how your site is set up, pushing an edited schedule — can overwrite
> exception schedules written from outside. Use booking schedules that
> operators don't edit in WebCTRL. If your team downloads routinely, either
> re-run the sync afterwards or schedule it to follow the maintenance window.
> Nothing detects this for you.

### Schneider EcoStruxure Building Operation

**Use the `bacnet` driver**, against the Schedule objects EBO exposes through
its BACnet Interface. Same two numbers: the device instance of the AS/AS-P, and
the schedule's object instance.

Older EBO buildings are often only schedulable at the floor or air-handler
level. That is fine — map those rooms with a `building:` (and a `floor:` where
the floor has its own AHU) and **no** `target:`, and their bookings drive the
roll-up.

If your site would rather drive EBO's own REST API — to keep the bookings as
native EBO objects, or because BACnet isn't permitted between those VLANs — use
the **`rest`** driver and paste your endpoints into `config.yaml`. Nothing about
your API is assumed; `config.example.yaml` has a commented starting template and
`bassync/drivers/rest.py` documents every placeholder. Prove it with
`--validate` before going live.

### Tridium Niagara

**Use the `bacnet` driver against the station's BACnet schedule export.** A
Niagara station's BACnet driver can export any schedule as a standard BACnet
Schedule object. The sync writes that object's `Exception_Schedule` like any
other controller's, and the station applies it to the BooleanSchedule as
**native special events — visible and editable in Workbench**. Nothing extra
is installed on the station; it is also the path commercial booking-to-HVAC
products use for Niagara.

Setting up one zone, in Workbench:

1. **Booking schedule.** Add a `BooleanSchedule` for the zone's bookings
   (say `Rm101_Booking`) with an empty weekly schedule and its default output
   `false`. Nothing else goes on it — the sync owns its special events.
2. **Combine.** Wire its output and the zone's normal schedule into an `Or`
   (kitControl) and use that as the zone's occupancy command. Holidays and
   shutdowns stay on the normal schedule, where the sync never writes.
3. **Export.** Under the station's `BacnetNetwork`, open the Local Device's
   **Export Table** (Bacnet Export Manager), discover the new schedule and
   add it. Note the exported Schedule object's instance number, and the Local
   Device's own device instance.
4. **Map it.** Give the room (or floor, or building) the target
   `"<station device instance>:<exported schedule instance>"` — e.g. `"2001:1"`
   — on a `bacnet` system. A station on the same network as your other
   controllers can share their `bacnet` system; one behind its own BBMD gets a
   system of its own.

Then run `--validate`. It reads every exported schedule back, reports its value
type, and warns about any special events already on it that a live run would
replace.

- The station must accept BACnet writes from the sync's address. If it's on
  another subnet, register through its BBMD (`bbmd_address`) or pin its
  address in the target (`2001:1@10.4.5.20`).
- A schedule that already carries many special events can be slow to accept a
  whole-array write. A dedicated booking schedule avoids that, and
  `operation_timeout` raises the ceiling if needed.
- For a station-side heartbeat, export a `NumericWritable` as an analog value
  and set `heartbeat_object: "2001:analog-value,<instance>"` on the system.

**The `niagara` driver is deprecated.** It wrote special events through a REST
service that stock Niagara 4 doesn't ship. Niagara's standard web API, oBIX,
reads and writes point values and invokes point actions, but we found no stock
way to create schedule special events through it. Existing configurations keep
working, with a warning in the log; see
[Upgrading](#moving-off-the-niagara-driver) to move a station to the BACnet
export.

### Any other BTL-listed controller

There is nothing special about the three above. If a device exposes a standard
Schedule object and lets you write `Exception_Schedule`, the `bacnet` driver
drives it — point a `target:` at `<device instance>:<schedule instance>` and
run `--validate`. If a write is silently ignored, `verify_writes` (on by
default) catches it by reading the array back.

### Networking for BACnet

- `local_address` must be a **real NIC address on this host**, with prefix
  length: `"10.4.1.55/24"`. The stack binds to it. If a BAS server on the same
  machine already owns UDP 47808, give the sync its own port:
  `"10.4.1.55/24:47809"`.
- If this host is **not on the controllers' subnet** — the usual case for a
  server writing to field panels — set `bbmd_address` to the BBMD for their
  network. The sync registers as a foreign device for the whole run, and
  `--validate` reports whether the BBMD acknowledged it.
- Pick a `device_id` that's free campus-wide and register it wherever your team
  tracks BACnet instance numbers.
- Pin addresses in targets (`12001:5@10.4.2.30`) on a large campus: it removes a
  broadcast round-trip per schedule and works where Who-Is doesn't cross subnets.
  The sync reads the device back at the pinned address before writing, so an
  address that has since moved to another controller fails instead of writing
  into it.
- A multistate occupancy schedule (UNSIGNED values) needs `occupied_value` and
  `unoccupied_value` on its system, because state numbers are site-specific.

### Running in a datacenter or the cloud

The sync talks to **the devices that hold the schedules you map, and nothing
else** — not every controller on campus. Where you put the booking schedules
decides what it needs to reach:

- **On servers** — a Niagara Supervisor or station exporting its booking
  schedules over BACnet, an EBO server through the `rest` driver — and the
  sync only needs UDP 47808 (or HTTPS) to those few addresses. The server
  passes the bookings on to its own controllers. This is the easiest layout
  for a VM on its own subnet.
- **In the field controllers** — WebCTRL keeps schedules in the controllers —
  and the sync needs to reach each controller that holds a mapped schedule,
  including through each building's BACnet router for MS/TP.

Validate lists every device and schedule it will use, reading each one back.

It does **not** need to be a BBMD. On a subnet of its own:

- **Pin addresses** in targets (`2001:1@10.20.0.15`). A pinned IP device is
  plain unicast: no broadcast, no BBMD.
- **Set `bbmd_address`** to any one existing campus BBMD for anything that
  needs broadcast — unpinned targets, and routed MS/TP devices (the
  `12001:5@2001:0x21` form), whose router is found by broadcast. The sync
  registers with it as a foreign device, and that BBMD relays to every subnet
  it peers with. That BBMD must accept foreign-device registrations; Validate
  says whether it did. Making the sync a BBMD itself would need every
  building's BBMD to list it as a peer, which foreign-device registration
  avoids.

And on the network side:

- **A routed private connection** to campus (site-to-site VPN, private link)
  with **no NAT** between the VM and the BAS: BACnet/IP carries IP addresses
  inside its packets, and NAT breaks them.
- **UDP 47808 both ways** between the VM and the devices (and the BBMD).
- **Never expose UDP 47808 to the internet** — BACnet/IP has no
  authentication. The same goes for the web UI's port without HTTPS.

## 25Live setup

- Create a **local** service account (not SSO) with read access to the relevant
  events and locations, and enable Series25 WebServices for it.
- Set `collegenet.instance` (CollegeNET-hosted) or `collegenet.base_url`
  (self-hosted).
- The request filters confirmed events by the numeric `state` parameter.
  **Instances differ in how they want it encoded** — set
  `collegenet.state_param_style` to `plus` (default), `space`, `comma`,
  `repeat`, or `none` (filter client-side). Getting this wrong returns 200 with
  zero events; `--validate` catches that and names a style that works.
- Individually cancelled occurrences of a recurring event (reservation state
  99) are skipped; `collegenet.exclude_reservation_states` changes the list.
- The fetch starts at yesterday's midnight, so an overnight booking that began
  last night is still scheduled for the rest of it this morning.
- Paging is guarded: if an instance ignores the paging parameters, the sync
  either takes the single full response or — if it would silently truncate —
  fails the fetch with a message, instead of re-requesting the same page.
- Find a space's numeric `space_id` from its detail-page URL in 25Live, or run
  `--discover`.

## Upgrading

### From 1.2

The sync itself is unchanged; what's new is how the container runs.

1. **The compose file runs the service** (`command: ["serve"]`): the sync on
   its schedule plus the web UI. Take the new `docker-compose.yml` and
   `.env.example`, and set `BAS_WEB_PASSWORD` in `.env`.
2. **The schedule moves into `config.yaml`** (`schedule:`, or the web UI's
   Schedule page). `SYNC_AT`/`SYNC_ON_START` still work and override it; remove
   them from `.env` to manage the schedule in the web UI. The default is the
   same nightly 02:00.
3. **`./config` is mounted read-write**, and the container runs as the folder's
   owner. If your 1.2 folder is root-owned, `sudo chown -R "$(id -u)" config`,
   or set `PUID`/`PGID`.
4. One-shot runs are `docker compose run --rm sync sync --validate` now that
   the service is the default command; the 1.x forms still work with
   `docker run`.

### From 1.1

Nothing to edit, but read these before the first run:

1. **Targets are schedules the sync owns.** This was always how the BACnet and
   Niagara drivers worked — each run replaces the exception list — but the docs
   used to say hand-entered exceptions would survive. They don't, and never
   did. Run `--validate`: it now warns about every target holding exceptions
   the sync didn't write. Move those to the zone's normal schedule, or give
   the sync dedicated booking schedules.
2. **BACnet values now match each schedule's type.** 1.1 always wrote BOOLEAN;
   ENUMERATED schedules now get ENUMERATED values. A multistate (UNSIGNED)
   schedule needs `occupied_value` / `unoccupied_value`, and `--validate` says
   so.
3. **The safety state moved** from `logs/last_run.json` to `state/last_run.json`.
   The old file is read once as the baseline, so nothing is lost. In Docker,
   mount the `state` volume (the new compose file does).
4. **A broken room-map row no longer stops the whole run.** The row, and the
   floor and building schedules it rolls up into, are left alone; the rest
   syncs, and the run exits `2`. Set `safety.on_map_errors: abort` to keep
   the old behaviour.
5. **Config is validated**, so a value that was silently ignored before (a
   misspelt timezone, a negative buffer) now stops the run with a message.
6. **The `niagara` driver is deprecated** — Niagara stations are driven
   through their BACnet schedule export (below). It still works, logs a
   warning, and now verifies TLS by default: set `verify_tls` to your
   station's CA bundle (or `false`, explicitly) until you move off it.
7. `rest` driver: `{target}` in a JSON payload is no longer percent-encoded,
   and a payload value that is exactly `"{value}"`, `"{index}"` or `"{count}"`
   is now sent as a JSON boolean/number instead of a string.

### Moving off the `niagara` driver

For each station, one schedule at a time if you like:

1. In Workbench, add each target's BooleanSchedule to the station's BACnet
   **Export Table** (see [Tridium Niagara](#tridium-niagara)) and note the
   exported instance numbers and the station's device instance.
2. Add (or reuse) a `bacnet` system that can reach the station, and change
   the building/floor/room from the `niagara` system to it, replacing each ORD
   target with `"<device instance>:<schedule instance>"`.
3. Replace `heartbeat_path` with a `heartbeat_object` on an exported point.
4. `--validate`, then `--dry-run`, then a live run; once nothing references
   the `niagara` system, delete it and its `BAS_SYS_<NAME>_PASSWORD`.

`--system <name>` lets you cut one station over and check it before the rest.
If these schedules already carry this sync's special events from the old
driver, the first BACnet write replaces them, as intended.

### From a pre-1.0 release

Existing beta/RC deployments keep working — the upgrade is additive:

- A pre-1.0 `config.yaml` with a top-level **`niagara:`** block is automatically
  promoted to `systems: {niagara: {driver: niagara, ...}}` and made the default
  (that driver is now deprecated — see above).
- `BAS_NIAGARA_PASSWORD` is still honored alongside the new
  `BAS_SYS_<NAME>_PASSWORD` form.
- **`niagara_path:`** in `space_mapping.yaml` is still read. The editor renames
  it to `target:` the next time you save.

Run `--validate` and `--dry-run` after upgrading, as always.

## Tests

The suite runs under pytest, offline — no 25Live, no BAS, no internet:

```bash
pip install -r requirements-dev.txt -c constraints.txt
python -m pytest            # or: python Test.py
ruff check . && mypy        # lint and type-check, as CI does
```

It covers:

- the merge/roll-up logic, and the loader and its inheritance rules
- config validation, the safety rail and its state file
- the 25Live client's paging, cancellation and fetch-window handling
- the email/webhook reports, the service's scheduler (DST included), its job
  runner and run history
- the web UI: sign-in and lockout, CSRF, every editing page, concurrent-edit
  and confirmation handling, and the sandboxed report view
- the editors' shared save logic

With BACpypes3 installed it also runs the **BACnet driver against simulated
controllers** over real BACnet/IP on loopback. Those tests cover value types,
stale-pin refusal, error handling, offline devices, foreign exceptions and the
heartbeat. CI runs everything on Python 3.13 and 3.14, with and without
BACpypes3, and also runs ruff, mypy, shellcheck, pip-audit, a package install
and a Docker build that starts the service and checks its web UI and health check.

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Contributions that add BAS drivers or support more 25Live configurations are
especially valuable. A new driver is one file implementing
[`ScheduleWriter`](bassync/drivers/base.py) plus a line in the registry.

## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
Copyright © 2026 Ryan Bibby and contributors.

## Disclaimer

This software writes occupancy schedules to building automation systems. **Test
thoroughly with `--validate` and `--dry-run`, and against a non-production
schedule, before going live.** Provided without warranty; you are responsible
for validating behavior against your own 25Live instance and your own BAS.

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI entry point from a checkout (the CLI itself is `bassync/cli.py`). |
| `bassync/` | The sync engine (importable, unit-tested). |
| `bassync/drivers/` | BAS integrations — `bacnet`, `rest`, `preview`, and the deprecated `niagara`. |
| `bassync/service.py` · `bassync/jobs.py` · `bassync/history.py` | The long-running service (schedule + web UI), its job runner, and the run history. |
| `bassync/web/` | The web UI (Flask): pages, templates and static files. |
| `bassync/mapedit.py` | Reading, checking and writing the settings files — shared by both editors. |
| `bassync/editor.py` · `editor.py` | The desktop editor (Tkinter), and its launcher. |
| `Edit-Rooms.bat` | Double-click launcher for the editor (Windows). |
| `config.example.yaml` | Connection/system template → copy to `config.yaml`. |
| `defaults.example.yaml` | Scheduling defaults → copy to `defaults.yaml`. |
| `space_mapping.example.yaml` | Room map template → copy to `space_mapping.yaml`. |
| `requirements.txt` · `requirements-bacnet.txt` · `requirements-web.txt` | Core dependencies · the BACnet driver's · the web UI's. |
| `constraints.txt` | Exact tested versions of every dependency. |
| `requirements-dev.txt` | Test and lint tools. |
| `pyproject.toml` | Package metadata (`pip install .`) and tool settings. |
| `tests/` · `Test.py` | The pytest suite · a `python Test.py` shortcut to it. |
| `Dockerfile` · `docker-compose.yml` · `docker-entrypoint.sh` | Container image, compose service, and entrypoint (`serve` or one-shot `sync`). |
| `.env.example` | Docker secrets template → copy to `.env`. |
| `CONTRIBUTING.md` · `CHANGELOG.md` · `LICENSE` | |
| `.github/` | CI and release workflows, the release helper script, and Dependabot configuration. |
