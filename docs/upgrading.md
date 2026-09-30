[Documentation](README.md) › Upgrading

# Upgrading

Read the section for the version you're coming from, and each one after it.
Run `--validate` and `--dry-run` after upgrading, as always. Every release's
changes are in [CHANGELOG.md](../CHANGELOG.md).

- [From 1.4](#from-14)
- [From 1.3](#from-13)
- [From 1.3.0rc1](#from-130rc1)
- [From 1.2](#from-12)
- [From 1.1](#from-11)
- [Moving off the niagara driver](#moving-off-the-niagara-driver)
- [From a pre-1.0 release](#from-a-pre-10-release)

## From 1.4

Nothing to edit, and nothing the sync writes changes until you add a
`low_temp_target:` to the room map. In the web UI:

1. **The built-in Basic and Advanced roles can see the new Schedules pages**
   (*See what's scheduled*). If you've changed a role on the Access page, it's
   saved in `web.yaml` and doesn't pick that up: tick *See what's scheduled*
   for it if you want it to.
2. **Marking events low temp and posting announcements are Admin's** until you
   give them to a role on the Access page.

## From 1.3

Nothing to edit. Two things behave differently:

1. **Syncing one system is part of *Sync now*.** It used to need *Run the
   tools* as well; now any role that can sync may sync one system or one
   building — a part of what it could already sync. To keep a role to some
   systems or buildings, [limit it](web-ui.md#limiting-what-a-role-may-sync).
2. **A sync limited to one system (or building) no longer sends the monitoring
   success ping** (`monitoring.ping_url`); only full syncs do, so a dead-man's
   switch notices when the scheduled sync stops. Failures still ping
   `ping_fail_url`.

## From 1.3.0rc1

Nothing to edit. Two things behave differently:

1. **A building or floor with no rooms rolling up into it is now written** —
   cleared when nothing books it, and checked by `--validate` — so that extra
   bookings can drive a building that isn't in 25Live. Before, such an entry
   was never touched. Run `--validate` and make sure each one points at a
   booking schedule the sync may own.
2. **Roles are capabilities now.** Basic, Advanced and Admin do what they did,
   plus the new pages: Advanced can edit extra bookings; Admin can use the
   Safety and Service pages. Someone in several groups gets every capability
   of every role they're in — with the built-in roles, the same as before.

## From 1.2

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

## From 1.1

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

## Moving off the `niagara` driver

For each station, one schedule at a time if you like:

1. In Workbench, add each target's BooleanSchedule to the station's BACnet
   **Export Table** (see [Tridium Niagara](bas-setup.md#tridium-niagara)) and
   note the exported instance numbers and the station's device instance.
2. Add (or reuse) a `bacnet` system that can reach the station, and change
   the building/floor/room from the `niagara` system to it, replacing each ORD
   target with `"<device instance>:<schedule instance>"`.
3. Replace `heartbeat_path` with a `heartbeat_object` on an exported point.
4. `--validate`, then `--dry-run`, then a live run; once nothing references
   the `niagara` system, delete it and its `BAS_SYS_<NAME>_PASSWORD`.

`--system <name>` lets you cut one station over and check it before the rest.
If these schedules already carry this sync's special events from the old
driver, the first BACnet write replaces them, as intended.

## From a pre-1.0 release

Existing beta/RC deployments keep working — the upgrade is additive:

- A pre-1.0 `config.yaml` with a top-level **`niagara:`** block is automatically
  promoted to `systems: {niagara: {driver: niagara, ...}}` and made the default
  (that driver is now deprecated — see above).
- `BAS_NIAGARA_PASSWORD` is still honored alongside the new
  `BAS_SYS_<NAME>_PASSWORD` form.
- **`niagara_path:`** in `space_mapping.yaml` is still read. The editors rename
  it to `target:` the next time you save.
