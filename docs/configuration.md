[Documentation](README.md) › Configuration

# Configuration

Everything site-specific lives in YAML files, each started from an example in
the repository. With Docker they're in `./config` and the
[web UI](web-ui.md) edits them; without it, edit them by hand or with the
[desktop editor](command-line.md#the-desktop-editor).

| File | Holds | Started from |
|---|---|---|
| `config.yaml` | Your 25Live instance, BAS systems, accounts, timezone, safety limits, alerting and the schedule. Usually IT-managed. | `config.example.yaml` |
| `defaults.yaml` | The operator-tunable defaults: run-up, run-down, merge gap, lookahead. | `defaults.example.yaml` |
| `space_mapping.yaml` | The room map: buildings, floors and rooms, and the schedule each one drives. | `space_mapping.example.yaml` |
| `extra_bookings.yaml` | Bookings that aren't in 25Live — see [Extra bookings](#extra-bookings). | written by the web UI (optional) |
| `web.yaml` | The web UI's sign-in, roles and branding — see [The web UI](web-ui.md). | written by the web UI |

Each example documents every setting it takes. Passwords are **never** in any
of them — see [Secrets](#secrets).

- [Settings — config.yaml](#settings--configyaml)
- [Secrets](#secrets)
- [Scheduling defaults — defaults.yaml](#scheduling-defaults--defaultsyaml)
- [The room map — space_mapping.yaml](#the-room-map)
- [Targets](#targets)
- [A broken row doesn't cost you the campus](#a-broken-row-doesnt-cost-you-the-campus)
- [Extra bookings](#extra-bookings)

## Settings — config.yaml

```bash
cp config.example.yaml config.yaml
```

`config.yaml` holds your 25Live instance, your BAS systems, accounts,
timezone, safety limits, alerting and the schedule. It's **gitignored**. Point
elsewhere with `--config PATH` or `$BAS_CONFIG`.

Every value is checked at startup. A wrong type or a misspelt timezone stops the
run with one line naming the key; a key the sync doesn't recognise (usually a
typo, like `notify_on_sucess`) is logged as a warning.

Its main sections:

- **`collegenet:`** — the 25Live instance and account; see
  [25Live setup](25live.md).
- **`systems:`** — one entry per BAS system, each with a `driver:` (`bacnet`,
  `rest`, `preview`); see [BAS setup](bas-setup.md) and
  [Networking](networking.md). `default_system:` names the one buildings use
  when they don't say. `python main.py --list-drivers` lists the drivers.
- **`timezone:`** — the campus timezone (IANA name, e.g. `America/New_York`).
- **`safety:`** — the mass-clear limits; see [Safety rails](safety.md).
- **`alerts:`** and **`monitoring:`** — email, webhooks and the dead-man's
  switch; see [Reports and alerts](reports-and-alerts.md).
- **`schedule:`** — when the service runs the sync: `times` (one or more
  `HH:MM`), `enabled`, and `run_on_start`. The web UI's Schedule page edits it.
- `log_file`, `log_max_mb`, `log_backups`, `retry` — logging and retries.

## Secrets

Passwords are **never** stored in the settings files. Set them in the
environment — or, with the web UI, on its Connection, Alerts and Access pages,
which keep them in `state/secrets.json`, readable only by the service. A
variable set in the environment always wins:

```bash
# Linux / macOS
export BAS_25LIVE_PASSWORD=...
export BAS_SYS_EBO_WEST_PASSWORD=...       # one per system that logs in (rest)

# Windows (PowerShell)
$env:BAS_25LIVE_PASSWORD = "..."
$env:BAS_SYS_EBO_WEST_PASSWORD = "..."
```

| Variable | For |
|---|---|
| `BAS_25LIVE_PASSWORD` | The 25Live service account. |
| `BAS_SYS_<NAME>_PASSWORD` | Each BAS system that logs in (the `rest` driver). The name is the system's key, uppercased, with non-alphanumerics as underscores: `ebo_west` → `BAS_SYS_EBO_WEST_PASSWORD`. |
| `BAS_SMTP_PASSWORD` | Alert email, if the account needs one. |
| `BAS_ALERT_WEBHOOK_URL` | Optional: the alert webhook, if you'd rather not keep it in the file. |
| `BAS_WEB_SSO_CLIENT_SECRET` | Optional: the Entra app's client secret for the web UI. |

BACnet systems — including Niagara stations reached through their BACnet
export — need no credential. In Docker, these go in `.env`; see
[Environment variables](docker.md#environment-variables).

## Scheduling defaults — defaults.yaml

```bash
cp defaults.example.yaml defaults.yaml
```

The operator-tunable file, kept separate from the IT-managed connection
settings:

| Setting | Meaning |
|---|---|
| `pre_condition_minutes` | Start each booking this early, so the room is comfortable when people arrive. |
| `post_buffer_minutes` | Hold each booking this long after it ends. |
| `merge_gap_minutes` | Join two windows within this many minutes of each other, rather than cycle the equipment off between them. |
| `lookahead_days` | How many days ahead to schedule. |

The first three can be overridden per building and per room
(**room > building > global**). Edit it on the web UI's Defaults page, in the
desktop editor's **Defaults** tab, or by hand.

<a id="the-room-map"></a>
## The room map — space_mapping.yaml

```bash
cp space_mapping.example.yaml space_mapping.yaml
```

Three sections: `buildings:` (each roll-up schedule, defined once, with any
equipment its rooms share), `floors:` (optional per-floor corridor schedules)
and `spaces:` (the rooms). Each room names its `building:` by id, and **every
room in a building is automatically unioned into that building's schedule** —
you never repeat the building's address on a room, so you can't forget to wire
one up.

```yaml
buildings:
  - id: liberal_arts
    name: "Liberal Arts"
    campus: Main                  # optional label for people; the sync ignores it
    system: webctrl
    target: "12200:100"           # the building's common-area schedule
    pre_condition_minutes: 40     # bigger air handler, longer run-up
    equipment:
      - id: ahu_3                 # rooms list it by this
        name: "AHU-3"
        target: "12200:300"       # runs whenever any room listing it is booked

floors:
  - building: liberal_arts
    level: 2
    target: "12200:120"           # the second-floor corridor

spaces:
  - space_id: 2201                # from 25Live
    space_name: "Liberal Arts 201"
    building: liberal_arts
    floor: 2                      # drives the floor 2 corridor too
    target: "12200:5"             # its own schedule; omit to drive only the roll-ups
    equipment: [ahu_3]            # and the AHU it shares with other rooms
```

Each entry carries:

- **`system:`** — which BAS it lives on, a key from `systems:` in `config.yaml`.
  Rooms and floors inherit their building's; buildings fall back to
  `default_system`. With one system defined you can omit it everywhere.
- **`target:`** — the schedule's address *within* that system (see
  [Targets](#targets)). **Required on a building or floor** — those entries
  exist to name a schedule. **Optional on a room:** omit it for a building
  scheduled per floor or per air handler, and the room drives its roll-ups
  instead. See [How finely can you schedule?](how-it-works.md#how-finely-can-you-schedule)
- **`floor:`** (rooms) — with a `floors:` entry of the same `building` and
  `level`, the room also drives that floor's corridor schedule. Occupancy rolls
  up **room → floor → building**: a corridor runs if any room off it is booked,
  and the building runs if any floor is.
- **`equipment:`** — on a building, a list of the equipment in it that has a
  schedule of its own: an air handler that serves several rooms, or a second VAV
  in a big room. Each entry has an `id` (unique in the building), an optional
  `name`, a `target`, and optionally a `system` (else the building's). On a
  room, the ids of the building's equipment that serve it, e.g.
  `equipment: [ahu_3, vav_2_14b]`. Each piece of equipment runs whenever any
  room that lists it is booked, merged across rooms like a floor; a room can
  list several, with or without a `target:` of its own. Equipment no room lists
  yet is still managed, and cleared. In the web UI it's **Room map →
  Equipment**, and ticked on each room's form.
- **`campus:`** (buildings) — an optional label. The web UI shows it on every
  room-map list, filters by it and counts rooms per campus; the sync ignores
  it, as 25Live has no campus to match it against.
- **`pre_condition_minutes`**, **`post_buffer_minutes`**, **`merge_gap_minutes`**
  (rooms and buildings) — override the [defaults](#scheduling-defaults--defaultsyaml).
- **`space_id:`** on a building — for a building whose common area is itself
  bookable in 25Live (an atrium, say).
- **`note:`** — free text for people; the sync ignores it.

Every building and floor entry names a schedule the sync owns, whether or not
any room rolls up into it: with no bookings it's cleared, like any other. A
building with no rooms at all — one that isn't in 25Live — can still be driven
by [extra bookings](#extra-bookings).

`space_mapping.example.yaml` documents every field and shows all three
granularity patterns side by side. Find a room's `space_id` with *Discover
spaces* in the web UI or `--discover` on the command line. The web UI's
[setup guide](web-ui.md#the-setup-guide) adds rooms found in 25Live with their
buildings in one go.

## Targets

| driver | target | meaning |
|---|---|---|
| `bacnet` | `12001:5` | device instance 12001, Schedule object instance 5 |
| `bacnet` | `12001:5@10.4.2.30` | …with the address pinned, skipping Who-Is |
| `bacnet` | `12001:5@2001:0x21` | …a routed MS/TP device: network 2001, MAC 0x21 |
| `bacnet` | `2001:1` | a Niagara station's exported schedule — see [Tridium Niagara](bas-setup.md#tridium-niagara) |
| `niagara` *(deprecated)* | `Bldg/Rm101_Occ` | ORD relative to `schedule_base_path` |
| `rest` | whatever your path template expects | see `bassync/drivers/rest.py` |

A BACnet pin belongs to the *device*: pin it once and every target on that
device uses it. Two spellings of the same schedule (`12001:5` and
`12001:5@10.4.2.30`) are recognised as one and written once.

## A broken row doesn't cost you the campus

A row the loader can't use — a non-numeric buffer, a malformed target, a floor
of a building that no longer exists — is reported and left out, the rest of
the campus syncs, and the run exits `2` so the alert fires.

Nothing the broken row affects is touched that night: not its own schedule,
and not the floor or building schedules it rolls up into, which keep their
current schedule rather than being rewritten without its bookings. A room
whose own target is malformed still feeds its floor and building.

Set `safety.on_map_errors: abort` to write nothing until the map is fixed
instead.

## Extra bookings

Occupancy the sync should schedule that 25Live doesn't know about: an open
house, an evening custodial shift, a make-up lab, a building that isn't in
25Live at all. Add them on the web UI's **Bookings** page, or in
`extra_bookings.yaml` beside `config.yaml` (`$BAS_EXTRA_BOOKINGS` or
`extra_bookings_file:` in `config.yaml` point elsewhere):

```yaml
bookings:
  - title: Open house
    building: science_hall      # a building's common-area schedule
    date: 2026-10-05            # one day...
    start: "08:00"
    end: "14:00"
  - title: Evening custodial
    building: liberal_arts
    floor: 2                    # ...a floor's corridor schedule
    days: [mon, wed]            # ...or every week on these days,
    from: 2026-09-01            #    between these dates (both optional)
    until: 2026-12-12
    start: "18:00"
    end: "21:00"
  - title: Chem lab make-up
    space_id: 1234              # a room in the room map, by its 25Live id
    date: 2026-10-07
    start: "13:00"
    end: "16:00"
    exact: true                 # no run-up or run-down
```

Each run treats them like 25Live bookings:

- **A room's** gets the room's run-up and run-down, and keeps its floor and
  building running too. **A building's or floor's** gets the building's.
  `exact: true` leaves the run-up and run-down off.
- An `end` at or before the `start` runs past midnight; `"24:00"` is midnight
  at the end of the day. Times are in the campus `timezone:`.
- They're scheduled up to `lookahead_days` ahead, like everything else, and a
  change is written at the next sync.
- They never count as 25Live bookings, so they can't hide a 25Live outage from
  the [safety check](safety.md).
- A row that won't read — a missing time, a day that isn't one — is reported,
  the run alerts (exit `2`), and the schedules it would have driven keep their
  current schedule that night, [like a broken room-map row](#a-broken-row-doesnt-cost-you-the-campus).
  A row naming a room or building the map doesn't have is a warning. A file
  that can't be read at all stops the run before anything is written.

`--validate` checks them, and the run report counts them. On the web UI, seeing
them needs *See everything* and changing them *Edit extra bookings*, which
Advanced has.
