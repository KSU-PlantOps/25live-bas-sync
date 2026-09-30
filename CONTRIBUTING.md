# Contributing

Thanks for helping improve 25Live → BAS Schedule Sync! This started as a
single-campus, single-vendor integration and the goal is to make it work
cleanly for any 25Live site, on any building automation system.

## Ways to help

- **New BAS drivers** — one file implementing `ScheduleWriter`
  (`bassync/drivers/base.py`) plus a line in the registry. Declare its
  settings in `config_keys`/`check_config`, canonicalise targets in
  `normalize_targets` if two strings can name one schedule, and document the
  `target:` syntax operators will type into the room map in its module
  docstring.
- **Adapt to other configurations** — different Niagara web services, different
  Series25 instances, controllers with unusual `Exception_Schedule` limits,
  other timezones/locales.
- **Bug reports** — include your Python version, OS, which driver you're using,
  a redacted snippet of the relevant 25Live XML or BAS response, and the log
  output from a `--verbose` run.
- **Docs** — clarify setup for a configuration you got working. The
  documentation is in `docs/`; the README is the landing page.

Please **do not** include real credentials, hostnames, or full data dumps in
issues or PRs.

## Development setup

```bash
git clone https://github.com/KSU-PlantOps/25live-bas-sync.git
cd 25live-bas-sync
python3.14 -m venv .venv && . .venv/bin/activate   # (.venv\Scripts\activate on Windows)
# Python 3.13+ required, 3.14 recommended; CI tests both.
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
pip install -r requirements-bacnet.txt -c constraints.txt   # the bacnet driver
pip install -r requirements-web.txt -c constraints.txt      # the web UI / service
cp config.example.yaml config.yaml              # edit for your test instance
cp space_mapping.example.yaml space_mapping.yaml
```

Run the offline tests, the linter and the type checker — the same three CI
runs (no 25Live and no BAS required):

```bash
python -m pytest            # or: python Test.py
ruff check .
mypy
```

With BACpypes3 installed, `tests/test_bacnet_device.py` runs the BACnet driver
against simulated controllers over loopback — extend it when you change what
goes on the wire.

Run the web UI against your test files (the schedule runs too — turn it off
on the Schedule page if you don't want it to):

```bash
BAS_WEB_PASSWORD=dev-password-123 BAS_WEB_HOST=127.0.0.1 python -m bassync.service
```

Validate end-to-end safely (neither mode writes anything):

```bash
python main.py --validate   # config, auth, bookings, reachability, targets
python main.py --dry-run    # talks to 25Live only; contacts no BAS at all
```

## Tests

The suite runs under pytest, offline — no 25Live, no BAS, no internet. It
covers:

- the merge/roll-up logic (equipment included), and the loader and its
  inheritance rules
- config validation, the safety rail and its state file, and a sync limited
  to one system or building
- the 25Live client's paging, cancellation and fetch-window handling, and
  what discovery collects (every space, or the booked ones) and guesses
- extra bookings: reading them, their occurrences and buffers, and a whole
  run with them
- low temp: the room map's low-temp schedules, marked events driving them,
  and what each sync records per space
- the email/webhook reports, the service's scheduler (DST included), its job
  runner, run history, restarting in place, and the update check
- the web UI: sign-in and lockout, Entra sign-in, roles, capabilities and
  sync limits, CSRF, every editing page (equipment included), the setup guide
  from nothing to a schedule, adding rooms from 25Live a building at a time,
  the Schedules pages and marking events low temp, announcements,
  concurrent-edit and confirmation handling, branding, and the sandboxed
  report view
- the editors' shared save logic
- the release helper (`.github/scripts/release_info.py`), and every link in
  the documentation

With BACpypes3 installed it also runs the **BACnet driver against simulated
controllers** over real BACnet/IP on loopback. Those tests cover value types,
stale-pin refusal, error handling, offline devices, foreign exceptions and the
heartbeat. CI runs everything on Python 3.13 and 3.14, with and without
BACpypes3, and also runs ruff, mypy, shellcheck, pip-audit, a package install
and a Docker build that starts the service and checks its web UI and health
check.

## Project layout

| Path | Purpose |
|---|---|
| `main.py` | CLI entry point from a checkout (the CLI itself is `bassync/cli.py`). |
| `bassync/` | The sync engine (importable, unit-tested). |
| `bassync/drivers/` | BAS integrations — `bacnet`, `rest`, `preview`, and the deprecated `niagara`. |
| `bassync/service.py` · `bassync/jobs.py` · `bassync/history.py` | The long-running service (schedule + web UI, restarting in place), its job runner, and the run history. |
| `bassync/extras.py` | Extra bookings: occupancy that isn't in 25Live. |
| `bassync/lowtemp.py` · `bassync/scheduled.py` | Events marked low temp; and what each sync wrote, per space, for the Schedules pages. |
| `bassync/discovery.py` | What `--discover` found, kept for Room map → From 25Live and the setup guide, and its guesses at each room's building. |
| `bassync/updates.py` | Whether a newer release is out (GitHub's releases API). |
| `bassync/web/` | The web UI (Flask): pages (`views.py`, `bookings.py`, the setup guide in `setup.py`, adding rooms from 25Live in `importer.py`, what's scheduled and low temp in `schedules.py`, announcements in `announce.py`), templates and static files; `access.py` (roles, capabilities and branding) and `entra.py` (Microsoft sign-in). |
| `bassync/secretstore.py` | Passwords set on the web UI, kept in `state/secrets.json`. |
| `bassync/mapedit.py` | Reading, checking and writing the settings files — shared by both editors. |
| `bassync/editor.py` · `editor.py` · `Edit-Rooms.bat` | The desktop editor (Tkinter), its launcher, and a double-click launcher for Windows. |
| `*.example.yaml` | The settings templates: `config`, `defaults` and `space_mapping`. |
| `requirements*.txt` · `constraints.txt` | Dependencies (core, BACnet, web UI, dev), and the exact versions CI tested. |
| `pyproject.toml` | Package metadata (`pip install .`) and tool settings. |
| `tests/` · `Test.py` | The pytest suite, and a `python Test.py` shortcut to it. |
| `Dockerfile` · `docker-compose.yml` · `docker-entrypoint.sh` · `.env.example` | The container image, the compose service, its entrypoint (`serve` or one-shot `sync`), and the secrets template. |
| `contrib/systemd/` | A host-side timer that keeps the container on the newest patch release. |
| `docs/` | The documentation. `docs/images/` holds the screenshots, taken from a demo site, and `social-preview.png`, the repository's social preview (Settings → General). |
| `.github/` | CI and release workflows, the release helper script, Dependabot, and the issue and pull request templates. |

## Guidelines

- **Keep the core generic.** Anything institution-specific belongs in
  `config.yaml`/`space_mapping.yaml`; anything vendor-specific belongs in a
  driver, not in the shared pipeline.
- **Add a test** for logic changes — `tests/` covers the pure logic without
  network access; follow that pattern. The example YAML files are tested to
  load without a single warning, so keep them in step with new settings.
- **Flag, don't hardcode, instance-specific assumptions.** Where a 25Live or
  vendor contract may vary, make it a config key with a documented default
  rather than a constant — as done for `rest_base`, `special_event_type` and
  `state_param_style`.
- **Never let a write path fail open.** Anything that could clear a schedule
  when it didn't mean to needs a guard; see `bassync/safety.py` for why.
- Match the surrounding style; keep functions small and commented where the
  *why* isn't obvious.

## Pull requests

1. Branch from `main`.
2. Make focused changes; run `python -m pytest`, `ruff check .` and `mypy`.
3. Describe what changed, why, and anything reviewers should verify against a
   live instance.

## Releasing

Releases are published by `.github/workflows/release.yml` when a new version
reaches `main`; nobody tags or uploads anything by hand.

1. In a pull request, set `__version__` in `bassync/__init__.py` to the new
   version — `X.Y.Z`, or `X.Y.ZrcN` for a pre-release — and add its section at
   the top of `CHANGELOG.md`:

   ```markdown
   ## [1.6.0] — 2026-12-01 — A short title for the release
   ```

   If the changelog has an `## [Unreleased]` section, that becomes it. The
   title after the date is optional and becomes the release's name. The
   section's text becomes the release notes. A test fails if the section is
   missing; `python .github/scripts/release_info.py check` says so directly.
2. That PR gets a dry run of the release: the wheel and both Docker images are
   built and the notes are shown in the run summary, but nothing is published.
3. Merge. The workflow re-runs CI on the merged commit, pushes the image to
   `ghcr.io/ksu-plantops/25live-bas-sync` as `:X.Y.Z`, `:X.Y` and `:latest`,
   then creates the `vX.Y.Z` tag and the GitHub release, with the wheel,
   source archive and checksums attached.

The tag is created last, so a failed run publishes no release: fix the cause,
then re-run the workflow from the Actions tab. A pre-release is marked as one
and gets only its own image tag, and a patch for an older line (1.1.1 after
1.2.0) doesn't move `:latest`.

By contributing, you agree your contributions are licensed under the project's
GPL-3.0 license.
