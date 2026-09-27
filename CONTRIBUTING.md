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
- **Docs** — clarify setup for a configuration you got working.

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

Validate end-to-end safely (neither mode writes anything):

```bash
python main.py --validate   # config, auth, bookings, reachability, targets
python main.py --dry-run    # talks to 25Live only; contacts no BAS at all
```

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

By contributing, you agree your contributions are licensed under the project's
GPL-3.0 license.
