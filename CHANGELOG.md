# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning from 1.0 onward.

## [Unreleased]

### Added
- **A setup guide in the web UI.** A new install opens on it: 25Live (saved
  and tested), the campus timezone and defaults, a BACnet system with this
  host's address filled in, the rooms, each building's schedule, then
  *Validate*, a *Dry run* and the schedule. It's a guided view over the usual
  settings files, so it can be left and picked up again, and it's under
  Settings. Nothing reaches the BAS before the end: the guide creates
  `config.yaml` with the schedule off, and imported buildings wait on a
  `preview` system called `staging` until they have a schedule.
- **Rooms found in 25Live, imported with their buildings.** The guide lists
  every space booked in the next 30–180 days, with its capacity and number of
  bookings, and a building taken from 25Live where the instance gives one, or
  guessed from the name ("Science Hall 204 (Chem lab)" → Science Hall).
  Ticked rooms are added in one go, creating their buildings.
- `--discover` collects each space's formal name, capacity, building and
  number of bookings (cancelled occurrences, and states the sync doesn't
  include, aren't counted), and keeps them in `state/discovery.json`. Its
  printed YAML is unchanged.

### Changed
- The status page's *Getting started* card follows the setup guide's steps,
  and goes once the guide is finished or skipped, or the site has run a live
  sync.

## [1.3.0] — 2026-09-28 — A web UI, and Docker as the way to run it

The container now runs a long-lived service with a web UI: status and
history, *Sync now*, and editing of the room map and every setting in the
browser, with Microsoft Entra ID sign-in and roles you can change. Docker
becomes the recommended way to run the sync. The sync engine is unchanged
apart from two additions: extra bookings, and every building and floor in
the room map being managed. Read *From 1.2* in `docs/upgrading.md` before
switching (and *From 1.3.0rc1* if you ran the release candidate).

### Added
- **Web UI**, served by the container: status (last sync, next run, room-map
  problems, missing passwords); sync history with each run's report and a CSV
  of every window; *Sync now* (optionally one system, optionally forced),
  *Dry run*, *Validate*, *Test alert* and *Discover spaces* with live output,
  and a way to stop a running job; editing of rooms, buildings, floors,
  connections and BAS systems, defaults and the schedule, with the same checks
  as the desktop editor and the sync; the settings files as text; the logs.
  Discovered 25Live spaces can be added as rooms in one click.
- **Sign-in with Microsoft Entra ID, and roles.** Entra groups, by object ID
  or name, get roles on the Access page. Basic (status, history, *Sync now*),
  Advanced (everything visible, the tools, editing the room map and extra
  bookings) and Admin (everything) come built in, and roles can be changed,
  added and deleted as a table of capabilities — see the basics, see
  everything, sync, run the tools, stop a job, force, edit the room map, edit
  extra bookings, edit settings, set passwords, see the activity log, restart,
  manage access. Someone in several groups gets every capability of every
  role they're in; changes apply to open sessions; a refused sign-in shows the
  groups its token carried. The local password can always do everything, and
  can be turned off once SSO works, but never so as to lock everyone out.
- **Extra bookings**: occupancy the sync schedules that isn't in 25Live — an
  open house, an evening shift, a building 25Live doesn't have. One day, or
  every week on chosen days between optional dates, on a room, a floor or a
  whole building, with or without the run-up and run-down. They're added on
  the **Bookings** page (or in `extra_bookings.yaml`), written like 25Live
  bookings, never counted as 25Live bookings by the safety check, counted in
  the run report and checked by `--validate`. A broken one is left out and
  holds the schedules it would drive, like a broken room-map row.
- **Alerts page**: email (SMTP server, security, account, recipients, report
  style, CSV), webhooks and dead-man's-switch pings, with *Send a test*.
- **Safety page**: the mass-clear check (on or off, the share of schedules one
  run may clear, the fewest 25Live bookings), what a broken row does, and
  retries, with the current baseline.
- **Service page**: restart the service from the web UI — in place, in a few
  seconds, nobody signed out, and not while a job runs or a scheduled sync is
  due; the version, and whether a newer release is out (checked on GitHub
  twice a day, and can be turned off), with a note for Admins on the status
  page; what's running, where the web UI listens, and when the HTTPS
  certificate expires.
- **Passwords from the web UI**: the 25Live, BAS-system, SMTP and SSO secrets
  can be set on the web UI and are kept in `state/secrets.json`, readable only
  by the service. The environment still wins, and every way of running the
  sync reads the stored ones.
- **Appearance**: a site name, logo (PNG, JPEG or WebP; SVG refused), accent
  colour with automatic text contrast, and a notice and contact details on the
  sign-in page and in the footer. An **Activity** log lists who did what.
- **Campus on buildings**: an optional label, shown and filterable on every
  room-map list and counted on the status page; the sync ignores it.
- Web UI security: every page and action checks a capability; five wrong
  local passwords lock the address out; signed sessions that end when the
  password or SSO app changes; CSRF tokens; a strict Content-Security-Policy;
  optional HTTPS (`BAS_WEB_TLS_CERT`/`_KEY`) or a trusted reverse proxy; every
  change logged with who made it. Edits made at the same time by two people
  are caught rather than one silently undoing the other.
- **The service** (`bas-sync-service`, the image's `serve`): runs the sync on
  its schedule and serves the web UI. Every sync and tool is the ordinary
  command in a process of its own, one at a time.
- **`schedule:` in config.yaml** — several times a day if you like, on or off,
  run on start — editable in the web UI. `SYNC_AT` still overrides it.
- **Run history**: every live sync saves its report in `state/runs/`.
- Docker: a health check (`bas-sync-service --health`), a graceful stop that
  lets a running sync finish, `PUID`/`PGID`, and a hardened compose file
  (read-only root filesystem, capabilities dropped, `no-new-privileges`, log
  rotation).
- `contrib/systemd/`: a host-side timer that keeps the container on the
  newest patch release of its minor version. The container can't safely
  update itself.
- **Documentation** in `docs/`, one page per topic — running with Docker, the
  web UI, configuration, BAS setup by vendor, networking (including running in
  a datacenter or the cloud, and why the sync registers as a foreign device
  rather than being a BBMD), 25Live, reports and alerts, the safety rails, the
  command line, and upgrading — with the README as a short landing page.
  Release notes link to the docs of their own version. A security policy
  (`SECURITY.md`), and issue and pull request templates.

### Changed
- **`docker-compose.yml` runs `serve`**, mounts `./config` read-write, and no
  longer sets `SYNC_AT`. The container runs as the owner of the config folder
  (or `PUID`/`PGID`), dropping root before anything else runs.
- Image: one-shot runs are `sync [args]`; 1.x-style flags, and no arguments at
  all, still behave as they did.
- **Every building and floor in the room map is managed**, whether or not any
  room rolls up into it: with no bookings its schedule is cleared, like any
  other, and `--validate` checks its target. Before, a building with no rooms
  was never written.
- The desktop editor and the web UI share one module for reading, checking and
  writing the settings files (`bassync/mapedit.py`).

### Fixed
- Saving a file from the desktop editor left it readable only by its owner
  (mode 0600), so a sync running as another user could no longer read it.
  Saves now keep the file's permissions.

### Since 1.3.0rc1
- Added: extra bookings and the Bookings page; roles you can change; the
  Safety and Service pages (restart, update check); the systemd update timer;
  the documentation in `docs/`.
- Changed: every building and floor is managed; each page checks a
  capability rather than a role; someone in several groups gets every
  capability of every role they're in.
- Fixed: the job runner could show a job finished before recording it and
  pruning old ones, so a job started straight after could have its files
  pruned; a contact email address broke across two lines on the sign-in page;
  the status page said missing passwords could only go in `.env`.

## [1.3.0rc1] — 2026-09-27 — Web UI, and Docker as the way to run it

The release candidate of 1.3.0; everything in it is listed under 1.3.0.

## [1.2.0] — 2026-09-27 — Hardening, email run reports, Niagara over BACnet

A hardening release from a full review of the code, deployment and docs, plus
**email run reports**. Several fixes change what gets written to a BAS; read
*Upgrading from 1.1* in the README before the first run.

### Added
- **Email run reports.** Every live run builds a report — outcome, safety
  verdict, each system's status, every warning and error, and every schedule
  with the exact windows written to it, grouped by day. Email gets it as text
  and HTML with a CSV of every window attached; on failure always, and every
  night with `alerts.email.notify_on_success`. Failure alerts now say what
  failed instead of "see the log". `--test-alert` sends a sample report.
- **Dead-man's switch.** `monitoring.ping_url` / `ping_fail_url` are fetched
  after each live run, so healthchecks.io, Uptime Kuma or Cronitor can alarm
  when the job stops running at all.
- **BACnet heartbeat** (`heartbeat_object`), matching the Niagara one.
- **Teams Workflows and generic JSON webhooks** (`alerts.webhook_format`), and
  a per-channel `notify_on_success`. `BAS_ALERT_WEBHOOK_URL` can hold the URL.
- **`--validate` checks that 25Live actually returns bookings** for the mapped
  rooms, and when it returns none, tries each `state_param_style` and names
  any that work. It also reports each BACnet schedule's value type, warns
  about exceptions a live run would replace, and checks the safety state is
  writable.
- **`--dry-run` shows the safety verdict** a live run would get.
- **One run at a time**: a second live sync exits `8` without writing.
- **Config validation.** Types, ranges, IANA timezones (including per
  system), known drivers and per-driver settings are checked at startup, with
  one clear message per problem; unknown keys (usually typos) are warned
  about. The room map warns about unknown keys too, with a suggestion — a
  misspelt `tagret:` used to turn a room silently into a roll-up-only room.
- `state_param_style: space`, and `collegenet.exclude_reservation_states`.
- `pip install .` gives `bas-sync` and `bas-sync-editor` commands;
  `constraints.txt` pins every dependency; the test suite moved to pytest
  (`python Test.py` still works); CI adds ruff, mypy, shellcheck, pip-audit, a
  package install and a Docker build; Dependabot keeps it all current.
- BACnet driver tests against **simulated controllers** over real BACnet/IP.
- **Automated releases.** Merging a version bump to `main` publishes the
  GitHub release, with the wheel and source archive attached, and a Docker
  image for amd64 and arm64 at `ghcr.io/ksu-plantops/25live-bas-sync`.

### Fixed
- **BACnet errors crashed the whole run.** BACpypes3 raises Error/Reject/Abort
  replies as `BaseException`, which slipped past every `except Exception` — so
  one offline controller or an access-denied write ended the run with a
  traceback, left the other systems unwritten, and sent no alert. They are now
  ordinary per-schedule failures.
- **BACnet values were always BOOLEAN.** Schedules driving binary points are
  usually ENUMERATED, and ASHRAE 135 requires exception values to match the
  schedule's type. The driver now reads `Schedule_Default` and writes the
  matching type; multistate schedules take `occupied_value` /
  `unoccupied_value`; `unoccupied_value: null` relinquishes instead.
- **A stale pinned address could write another controller's schedule.** The
  device at a pinned address is now read back and must be the device named in
  the target.
- **Two spellings of one BACnet schedule overwrote each other** (`12001:5` and
  `12001:5@10.4.2.30`). Targets are canonicalised per driver — including
  routed MS/TP pins, `2001:0x21` = `2001:33` — a pin applies to the whole
  device, and conflicting pins are an error.
- **The BACnet connect timeout didn't cover the socket bind** (BACpypes3 binds
  in the background), so a busy port failed every write one by one.
- **25Live paging could loop 1,000 times.** An instance ignoring the paging
  parameters returned the same page repeatedly — 150 events became 150,000 and
  defeated the `min_events` check. The fetch now stops on a complete response,
  fails loudly on a truncating one, and de-duplicates (which also fixes
  multi-room events counted once per 50-room batch).
- **Cancelled occurrences of recurring events still drove HVAC.**
- **Overnight events could be cut off at 2 AM** — the fetch now starts
  yesterday and clips to today.
- **The safety baseline silently disappeared in Docker** (a root-owned bind
  mount, or no mount at all). It now lives in `state/` with a named volume,
  is written atomically with a `.prev` backup, is read from the 1.x location
  once, and a run without one warns every time.
- **One broken room-map row stopped the whole campus.** Bad rows are now left
  out and the rest syncs (exit `2`); the floor and building schedules a broken
  row rolls up into are left as they are rather than rewritten without its
  bookings, a room with a malformed target of its own still feeds its
  roll-ups, and the safety baseline is merged so nothing left alone loses its
  history. `safety.on_map_errors: abort` restores the old behaviour.
- **One system's bad setting could abort every system** — only `DriverError`
  was contained. Any exception from a system now fails only that system.
- `send_alert` could raise on a malformed setting despite promising not to.
- **The Docker scheduler crash-looped on the spring-forward date** — 02:00
  doesn't exist, GNU `date` failed, the entrypoint exited. It is now
  `bassync.scheduler`, which runs a nonexistent time as the clock jumps and a
  repeated time once; `SYNC_AT` like `29:00` is rejected.
- **Editor:** Save silently picked a default system when several were defined —
  the exact guess the loader refuses to make. Deleting a building left its
  floors behind and could orphan rooms, stopping the next run. Editing a row
  dropped fields the form doesn't show. A `config.yaml` it couldn't parse was
  overwritten with just the form's fields. Every Save rewrote `config.yaml`
  and stripped its comments. The window froze during Preview and the
  connection tests.
- `--discover` printed invalid YAML for names with quotes or backslashes.
- An empty section in `config.yaml` (`alerts:` with nothing under it) crashed
  the run.

### Changed
- **Ownership model, documented.** The sync owns each target's exception list
  and replaces it every run — which is how the BACnet and Niagara drivers
  always worked, although the docs said hand-entered exceptions would survive.
  Give the sync dedicated booking schedules, combined with the normal
  schedule in the controller.
- **Niagara stations are driven through their BACnet schedule export** with
  the `bacnet` driver: the station applies the writes as native special
  events, visible in Workbench, with nothing extra on the station. The README
  has a step-by-step setup and a migration guide, and the examples show a
  station this way.
- **The `niagara` driver is deprecated.** It needs a REST service stock
  Niagara 4 doesn't ship (and oBIX, the standard web API, has no stock way to
  create special events). It keeps working for existing deployments, logs a
  deprecation warning, and now verifies TLS by default.
- `rest` driver: `{target}` is no longer percent-encoded inside JSON payloads,
  and a payload value that is exactly `"{value}"`, `"{index}"` or
  `"{count}"` is sent as a JSON boolean/number.
- Logs rotate (`log_max_mb`, `log_backups`).
- Removed `BAS_WEBCTRL_PASSWORD` / `BAS_EBO_PASSWORD`, "legacy" names for
  drivers that never existed. `BAS_NIAGARA_PASSWORD` is still honored.
- The CLI and editor moved into the package (`bassync/cli.py`,
  `bassync/editor.py`); `main.py`, `editor.py` and `Edit-Rooms.bat` are
  unchanged to run. Exit code `1` now also covers invalid configuration.

## [1.1] — 2026-09-08

An audit-and-cleanup release. Five bugs fixed, per-room/per-floor/per-building
scheduling made explicit, and the docs and examples re-pointed so the project
reads as what it is — a BACnet tool that happens to include a Niagara REST
driver — rather than a Niagara tool with BACnet bolted on.

### Added
- **Rooms may omit `target:`.** How finely a building can be scheduled depends
  on how it was built out, not on this tool. A room in a building that is only
  schedulable at the floor or air-handler level now maps with a `building:`
  (and optionally `floor:`) and no `target:` — its bookings drive the roll-ups
  and it writes no schedule of its own. All three patterns mix freely in one
  map; `space_mapping.example.yaml` shows them side by side. A room with
  neither a `target:` nor a `building:` is still an error, because its bookings
  would drive nothing.
- The editor shows such rooms as `(roll-up only)` rather than a blank cell, and
  its Target field is no longer marked required.

### Fixed
- **A per-system `timezone:` produced a schedule that never turned off.**
  Windows were split at the campus's midnight before being converted to the
  device's zone, so a building in another timezone got an exception that turned
  ON and had no matching OFF — the controller would have run until the next
  exception. Windows are now converted before they are split.
- **Decommissioning rooms tripped the mass-clear safety rail.** Schedules
  removed from the room map counted as "cleared" even though the run no longer
  writes them, blocking the next sync with a false alarm. The comparison is now
  scoped to the schedules the run actually manages — which also subsumes the
  separate `--system` scoping, so that special case is gone.
- **One malformed row took down the whole run.** A non-numeric
  `pre_condition_minutes`, `merge_gap_minutes` or `floor:`, or a list entry that
  isn't a mapping, raised out of the loader instead of being reported. Bad rows
  are now collected as errors like every other mapping problem, and the rest of
  the map still loads.
- **Out-of-range BACnet instances were accepted.** Instances are 22-bit, and
  4194303 is the reserved "unconfigured" value a controller reports before
  commissioning — never a real address. Both are now rejected when the target is
  parsed.
- **`space_id: 1234.0` silently never matched.** YAML reads an unquoted decimal
  as a float, and `str()` gave `"1234.0"`, which never matches the `"1234"`
  25Live sends — the room just never synced. Integral floats are normalised.

### Changed
- **Python 3.13 is now the minimum; 3.14 is recommended** and is what the
  Docker image runs. CI tests both.
- Documentation and examples are vendor-neutral. WebCTRL and EcoStruxure lead
  the per-vendor setup section, the examples show a multi-BACnet campus instead
  of a single Niagara station, and generic prose no longer uses Niagara as the
  reference platform. The `niagara` driver, `BAS_NIAGARA_PASSWORD` and
  `niagara_path:` are all unchanged and still supported.
- New README section, *How finely can you schedule?*, covering the per-room /
  per-floor / per-building decision and how to map each.

## [1.0] — 2026-09-08

**First stable release.** The sync is no longer Niagara-specific: it drives any
BTL-listed BAS over standard BACnet, so one nightly run can cover a mixed campus
of Tridium Niagara, Automated Logic WebCTRL and Schneider EcoStruxure.

Everything below is relative to **V1.0 RC1**. Existing RC/beta deployments keep
working untouched — see *Upgrading* at the end.

### Added
- **Pluggable BAS drivers.** `python main.py --list-drivers`:
  - **`bacnet`** — standard ASHRAE 135 Schedule objects over BACnet/IP, via
    [BACpypes3]. Covers Tridium Niagara, Automated Logic WebCTRL and Schneider
    EcoStruxure Building Operation with one code path. Writes only
    `Exception_Schedule`, one special event **per calendar date** (which keeps
    the array inside the limits real controllers enforce), at `eventPriority`
    16 so hand-entered exceptions always win. Optional foreign-device/BBMD
    registration, address pinning, and write-verification read-back.
  - **`niagara`** — the previous REST writer, with `rest_base`,
    `special_event_type` and the ORD style now settable from `config.yaml`.
  - **`rest`** — a generic driver whose endpoints and payloads you describe in
    `config.yaml`, for a vendor API when BACnet isn't available.
  - **`preview`** — writes nothing; logs and optionally exports CSV, for staged
    commissioning one building at a time.
- **`systems:` config block** and per-space `system:` / `target:` keys, so a
  mixed campus syncs in one run. Rooms inherit their building's system
  (precedence room > building > `default_system`).
- **Per-floor corridors work across vendors.** The `floors:` section from RC1
  now carries `system:` too and inherits from its building, so a part-finished
  retrofit can leave a corridor on the supervisor while its rooms move to
  BACnet. Floor schedules also join the managed set, so a corridor with no
  bookings is actively cleared instead of holding last week's occupancy.
- **Editor Connection tab is driver-aware.** Pick a system from the dropdown and
  the fields follow its driver — NIC address and BBMD for BACnet, host/port/ORDs
  for Niagara — with **Add…** to create one. Systems you aren't looking at, and
  config sections the form never shows (`retry`, `alerts`, `safety`), survive a
  save untouched.
- **Docker image carries the BACnet driver** and documents the host-networking
  requirement: BACnet binds a real NIC and needs broadcast, neither of which
  survives the default bridge. `.env` is passed through wholesale so per-system
  `BAS_SYS_<NAME>_PASSWORD` variables reach the container.
- **Mass-clear safety rails** (`safety:`). A run refuses to write if 25Live
  returned fewer than `min_events` assignments, or if more than
  `max_cleared_fraction` of previously-occupied schedules would be emptied at
  once — the signature of an auth failure or an API change, which would
  otherwise stand the whole campus down under a successful-looking run.
  `--force` overrides; new exit code `7`.
- **`--system NAME`** to limit a run to one BAS, and **`--list-drivers`**,
  **`--version`**, **`--verbose`**.
- `collegenet.state_param_style` (`plus`/`comma`/`repeat`/`none`) — Series25
  instances differ in how they want the `state` filter encoded, and getting it
  wrong returns 200 with zero events.
- Room-map validation now reports **every** problem at once: missing targets,
  duplicate `space_id`s, unknown `system:` names, unparseable YAML.
- Editor: **System** dropdown fed from `config.yaml`, and *Test BAS
  connections* health-checks every configured system in one pass.
- **`--test-alert`** sends a test notification through every configured channel
  and reports each result — usable while `alerts.enabled` is still false, so
  the plumbing can be proven before anyone depends on it.
- SMTP alerting gained implicit TLS (`security: ssl`, port 465) alongside
  STARTTLS, explicit `Date`/`Message-ID` headers so alerts aren't quarantined,
  and separate reporting for connect / TLS / authentication / refused-recipient
  failures. A `username` with no `$BAS_SMTP_PASSWORD` is now caught before
  connecting instead of being rejected by the relay with an opaque 5xx.
- CI runs the suite both with and without BACpypes3 installed.

### Fixed
- **Building roll-ups with no bookings were never cleared.** The clear-loop only
  considered room targets, so a building whose rooms all lost their bookings
  kept conditioning on the previous run's schedule indefinitely.
- **Two rooms sharing one schedule silently lost one room's bookings.** The
  builder assigned rather than unioned, so whichever room was processed second
  erased the first — a real pattern for a divisible room split A/B in 25Live but
  served by a single AHU. Their windows are now unioned, and the loader warns
  when it sees a shared target.
- **A crash sent two alerts** — one "CRASHED", then one "FAILED" for the same
  event.
- **`--validate` passed on a broken 25Live endpoint.** Any response at all was
  treated as success, so a 404 from a wrong `instance`/`base_url`, or an SSO
  login page where Series25 XML was expected, both reported PASS.
- A 25Live response that isn't XML (a login page, a proxy error) now reports
  what it actually got instead of raising a bare `ParseError`.
- Duplicate `space_id` elements within one reservation no longer produce
  duplicate events.
- Reservations that end at or before they start are skipped with a warning
  rather than producing a negative-length window.
- `verify_tls: false` no longer reaches through the deprecated
  `requests.packages.urllib3` path to silence warnings.

### Changed
- Code reorganised into the importable **`bassync/`** package; `main.py` is now
  the CLI. `Test.py` remains the test entry point.
- `event_priority` is documented and configurable, replacing the previous
  hard-coded `BACNET_SCHEDULE_PRIORITY = 14` — which conflated the
  BACnetSpecialEvent precedence (which exception wins) with the commandable
  priority array (which is a different mechanism, and one this tool never
  touches).
- Per-system passwords via `BAS_SYS_<NAME>_PASSWORD`; a password left in
  `config.yaml` now warns.
- **Python 3.11 is now the minimum**, checked at startup with an actionable
  message rather than a traceback from inside a dependency. 3.9 and 3.10 are
  past end of life and no longer receive security fixes, which matters for a
  process holding service credentials on a controls network. CI covers 3.11
  through 3.14.
- Repository renamed to **25live-bas-sync**.

### Upgrading from V1.0 RC1 or a beta
No configuration changes are required:
- A top-level `niagara:` block is promoted to `systems:` automatically and made
  the default.
- `BAS_NIAGARA_PASSWORD` and `niagara_path:` are both still honored; the editor
  renames `niagara_path:` to `target:` the next time it saves.

Two behaviour changes to expect on the first run:
1. **Building roll-ups with no bookings are now cleared.** Some buildings will
   genuinely stand down after the upgrade — that is the fix working, not a
   regression.
2. **The mass-clear safety rail is on by default.** A first run during a break
   may stop and exit 7. Read the log, then re-run with `--force` if the drop is
   real.

Run `--validate` and `--dry-run` first, as always.

[BACpypes3]: https://github.com/JoelBender/BACpypes3

## [V1.0 RC1] and earlier pre-releases

### Added
- **Docker support.** A `Dockerfile`, `docker-compose.yml`, and entrypoint run
  the headless sync in a container. One-shot by default (pass `--validate` /
  `--dry-run` straight through; ideal for host cron or a k8s CronJob) with an
  optional built-in daily scheduler (`SYNC_AT=HH:MM`) so `docker compose up -d`
  is a self-contained nightly sync. Secrets stay in the environment / a
  gitignored `.env`; config is mounted at `/config`. New `BAS_SPACE_MAP` env var
  (parallel to `BAS_CONFIG` / `BAS_DEFAULTS`) points the sync at its room map.
- **Editor: Connection tab + auto theme.** The GUI now edits `config.yaml`
  (25Live, Niagara, timezone) directly, so it's a one-stop shop — one Save writes
  the room map, connection settings, and defaults together. Passwords stay in env
  vars and unexposed sections (`retry`, `alerts`) are preserved. The window also
  follows the OS light/dark appearance.
- **Editor: usability pass.** Per-table live search, click-to-sort columns,
  vertical scrollbars, a Duplicate action, a right-click context menu, keyboard
  shortcuts (Enter to edit, Del to delete), a status bar with live counts, and
  tab labels that show item counts.
- **Per-floor hallway HVAC.** Optional `floors:` section (building + level +
  niagara_path) and a per-room `floor:`. A room drives its floor's hallway
  schedule, and floors roll up into the building (room → floor → building). New
  **Floors** tab and per-room floor field in the editor.
- **`defaults.yaml`** — global scheduling defaults (run-up, run-down, merge-gap,
  lookahead) split into their own operator-tunable file, with a **Defaults** tab
  in the editor and a `--defaults` flag. Connection/auth settings stay in
  `config.yaml`.
- **Per-building run-up/run-down override.** A building may set
  `pre_condition_minutes`/`post_buffer_minutes` that its rooms inherit;
  precedence is room > building > global default.
- **Retry with backoff** for transient 25Live/Niagara errors (timeouts, 429,
  5xx). Applied to reads and the idempotent clear; POST writes are not
  auto-retried, to avoid duplicate special events. Configurable via `retry:`.
- **Failure alerts** — webhook (Slack/Teams/generic) and/or SMTP email on a
  failed (or optionally successful) sync run. Configurable via `alerts:`; the
  SMTP password comes from `BAS_SMTP_PASSWORD`.
- **`--validate`** pre-flight mode: checks config, 25Live auth, Niagara
  reachability, and that every schedule ORD exists — without writing.
- **`--discover`** read-only mode: lists 25Live spaces with upcoming events as a
  starter for `space_mapping.yaml` (`--discover-days` sets the window).
- **GUI tools** in `editor.py`: *Test 25Live connection*, *Test Niagara
  connection*, and *Preview (dry run)*.
- GitHub Actions **CI** (byte-compile + offline tests on Python 3.9 and 3.12),
  `CONTRIBUTING.md`, `CHANGELOG.md`, and the full GPL-3.0 `LICENSE`.

### Changed
- **Open-source ready / institution-agnostic.** All site-specific settings moved
  out of `main.py` into `config.yaml` (copied from `config.example.yaml`, merged
  over built-in defaults by `load_config()`; `--config`/`$BAS_CONFIG` select the
  path). The room map ships as `space_mapping.example.yaml`. Both real files are
  gitignored.

### Fixed
- **Critical:** add `tzdata` so `zoneinfo` works on Windows (was crashing at
  startup).
- **High:** robust 25Live pagination — stop on a short page instead of relying on
  a `total_count` element, which could silently drop events past the first page.
- Honor explicit `0` for pre/post/merge-gap minutes (was coerced to the default).
- URL-encode Niagara ORDs so names with spaces don't malform request URLs.
- Interpret naive 25Live timestamps in the configured timezone, not the server's.
- Suppress per-request `InsecureRequestWarning` when `verify_tls` is false.
