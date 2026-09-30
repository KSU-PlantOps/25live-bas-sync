[Documentation](README.md) › BAS setup

# BAS setup, by vendor

Whichever system you're on, the sync populates the *calendar*. You still wire
the schedule into your equipment once, in the vendor's own tool:

1. For each level you intend to drive — per room, per floor, per building, or a
   mix; see [How finely can you schedule?](how-it-works.md#how-finely-can-you-schedule) —
   create a **dedicated booking schedule** for the sync.
2. Leave its weekly schedule empty or **Unoccupied** — the sync only writes the
   booking exceptions on top.
3. Combine it with the zone's normal schedule in your occupancy logic
   (room → that zone; floor → corridor AHU; building → common AHUs and lobbies),
   typically *occupied = normal OR booking*. Holidays and shutdowns stay on the
   normal schedule.

Then run `--validate`, which resolves every target, reads each one's value
type, and warns about exceptions a live run would replace — all without
writing. [The sync owns what it writes](how-it-works.md#the-sync-owns-what-it-writes)
explains why.

| System | Driver | Target |
|---|---|---|
| [Automated Logic WebCTRL](#automated-logic-webctrl) | `bacnet` | controller device instance : schedule instance |
| [Schneider EcoStruxure Building Operation](#schneider-ecostruxure-building-operation) | `bacnet`, or `rest` | AS/AS-P device instance : schedule instance |
| [Tridium Niagara](#tridium-niagara) | `bacnet`, through the station's BACnet export | station device instance : exported schedule instance |
| [Any other BTL-listed controller](#any-other-btl-listed-controller) | `bacnet` | device instance : schedule instance |

For reaching the controllers — subnets, BBMDs, a VM in a datacenter — see
[Networking](networking.md).

## Automated Logic WebCTRL

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

> [!WARNING]
> **An operational gotcha worth planning for:** WebCTRL treats its own
> database as the source of schedules. A **download** to a controller — and,
> depending on how your site is set up, pushing an edited schedule — can
> overwrite exception schedules written from outside. Use booking schedules
> that operators don't edit in WebCTRL. If your team downloads routinely,
> either re-run the sync afterwards or schedule it to follow the maintenance
> window. Nothing detects this for you.

## Schneider EcoStruxure Building Operation

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

## Tridium Niagara

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

> [!NOTE]
> **The `niagara` driver is deprecated.** It wrote special events through a
> REST service that stock Niagara 4 doesn't ship. Niagara's standard web API,
> oBIX, reads and writes point values and invokes point actions, but we found
> no stock way to create schedule special events through it. Existing
> configurations keep working, with a warning in the log; see
> [Moving off the niagara driver](upgrading.md#moving-off-the-niagara-driver)
> to move a station to the BACnet export.

## Any other BTL-listed controller

There is nothing special about the three above. If a device exposes a standard
Schedule object and lets you write `Exception_Schedule`, the `bacnet` driver
drives it — point a `target:` at `<device instance>:<schedule instance>` and
run `--validate`. If a write is silently ignored, `verify_writes` (on by
default) catches it by reading the array back.

A multistate occupancy schedule (UNSIGNED values) needs `occupied_value` and
`unoccupied_value` on its system, because state numbers are site-specific.

## A low-temp schedule

For rooms that sometimes need to run colder — a blood drive, a crowded exam —
the sync can drive a second schedule, active only during events
[marked low temp](configuration.md#low-temp). It's set up like a booking
schedule, on any vendor:

1. Create a **dedicated low-temp schedule** for the room (or the equipment that
   serves it), with its weekly schedule empty or inactive.
2. In the zone's program, **lower the cooling setpoint while it's active** —
   an offset, or a setpoint of its own. How much colder is decided here, in the
   BAS; the sync only writes when.
3. Name it as the room's (or equipment's) `low_temp_target:` in the room map,
   and run `--validate`.

The sync writes it exactly as it writes an occupancy schedule — active for
each booking of a marked event, with the room's run-up so the room is cold
when people arrive, and cleared otherwise — so the same value types
(`occupied_value`, for a multistate one) apply. Keep the room's occupancy
schedule as it is: a low-temp booking still runs the room through its
`target:` as usual.

## Adding a driver

A new BAS integration is one file implementing
[`ScheduleWriter`](../bassync/drivers/base.py) plus a line in the registry —
see [CONTRIBUTING.md](../CONTRIBUTING.md).
