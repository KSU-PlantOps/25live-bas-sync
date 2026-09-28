[Documentation](README.md) › The web UI

# The web UI

The container serves a web UI for running and configuring the sync: status
and history, *Sync now*, the tools with live output, and editing of the room
map and every setting — checked by the sync's own validation before anything
is saved. It follows the browser's light or dark setting.

- [Pages](#pages)
- [The setup guide](#the-setup-guide)
- [Adding rooms from 25Live](#adding-rooms-from-25live)
- [How it runs things](#how-it-runs-things)
- [Sign-in and roles](#sign-in-and-roles)
- [Changing the roles](#changing-the-roles)
- [Setting up Microsoft Entra sign-in](#setting-up-microsoft-entra-sign-in)
- [Security](#security)
- [Branding](#branding)
- [Without Docker](#without-docker)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/dashboard-dark.png">
  <img alt="The status page: last sync, next sync, room map summary, and the Sync now, Dry run, Validate, Test alert and Discover buttons" src="images/dashboard-light.png">
</picture>

## Pages

| Page | What it does |
|---|---|
| **Status** | Last sync and its result, the next scheduled run, room-map problems, missing passwords, and buttons for *Sync now* (optionally one system, optionally *Force*), *Dry run*, *Validate*, *Test alert* and *Discover spaces*. |
| **History** | Every live sync with the report it emailed — every schedule and the exact windows written — and a CSV of every window. |
| **Bookings** | [Extra bookings](configuration.md#extra-bookings): occupancy that isn't in 25Live — one day or every week, on a room, a floor or a whole building; add, edit, copy and delete, and clear out the ones that have ended. |
| **Jobs** | Every sync and tool, from the schedule or the web, with its full output, live while it runs. A running job can be stopped. |
| **Room map** → Rooms · Buildings · Floors · Equipment | The room map, with search, a campus filter and sortable columns; add, edit, copy and delete. [Equipment](configuration.md#the-room-map) — an AHU several rooms share, or a room's second VAV — is ticked on each room's form. Renaming a building or equipment repoints what uses it; deleting a building takes its floors and equipment and won't leave rooms driving nothing, and equipment in use can't be deleted. |
| **Room map** → From 25Live | The spaces in 25Live, booked or not, [grouped by building](#adding-rooms-from-25live), to add a building at a time. |
| **Settings** → Setup guide | A new install's [walk through setup](#the-setup-guide): 25Live, the campus, a BAS system, rooms found in 25Live, each building's schedule, and the checks before the schedule goes on. |
| **Settings** → Connection | 25Live, BAS systems (add, remove, change driver), timezone and default system; the passwords, set or cleared here. *Validate* per system. |
| **Settings** → Alerts | Email (SMTP server, security, account and password, recipients, full or summary report, CSV), webhooks (Slack, Teams, generic) and the dead-man's-switch pings; *Send a test*. |
| **Settings** → Schedule · Defaults | When the sync runs; the run-up/run-down/merge-gap/lookahead defaults. |
| **Settings** → Safety | The [mass-clear check](safety.md) (on or off, the share of schedules one run may clear, the fewest 25Live bookings), what a broken row does, and retries; the current baseline. |
| **Settings** → Files | `config.yaml`, `defaults.yaml`, `space_mapping.yaml` and `extra_bookings.yaml` as text, for anything the forms don't cover; a zip of them all. |
| **Settings** → Access | The roles and what each may do, signing in with Microsoft Entra ID, and which Entra groups get which role. |
| **Settings** → Appearance | Your site name, logo and accent colour, and a notice and contact details on the sign-in page and at the foot of every page. |
| **Settings** → Service | The version, and whether a newer release is out; *Restart the service*; what's running — since when, as whom, where the web UI listens, the HTTPS certificate's expiry, and where each file is. |
| **Logs** | The sync's and the service's logs, and **Activity**: who did what — sign-ins, refused sign-ins, every change and every job. |

| Room map | A sync's report |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="images/rooms-dark.png"><img alt="The rooms list, with building, campus, floor, system and target columns" src="images/rooms-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="images/report-dark.png"><img alt="A sync's report: its result, the safety check, and every schedule with the windows written" src="images/report-light.png"></picture> |
| **Extra bookings** | **Adding one** |
| <picture><source media="(prefers-color-scheme: dark)" srcset="images/bookings-dark.png"><img alt="The Extra bookings list: title, where, when, the next occurrence, run-up and who added it" src="images/bookings-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="images/booking-form-dark.png"><img alt="The extra booking form: title, where, one day or every week, date, times, exact times and a note" src="images/booking-form-light.png"></picture> |
| **A job's live output** | **Roles, as a table of capabilities** |
| <picture><source media="(prefers-color-scheme: dark)" srcset="images/job-dark.png"><img alt="A sync's output as it runs" src="images/job-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="images/access-dark.png"><img alt="The Access page: a column per role, a row per capability" src="images/access-light.png"></picture> |

## The setup guide

On a new install — no `config.yaml` yet — signing in opens the setup guide, for
anyone whose role can change settings. It walks through what a site needs, in
order, and each step shows as done once the files say so:

1. **25Live** — the instance (or a self-hosted base URL), the service account
   and its password, and which kinds of booking to include. *Save and test*
   makes one small request, and says why if 25Live refuses it.
2. **Campus** — the timezone (this browser's is suggested), the lookahead, and
   the run-up, run-down and merge-gap defaults.
3. **BAS system** — a `bacnet` system: this host's address (filled in from its
   main network; check the prefix), a device ID nothing else uses, and a BBMD
   if the controllers are on another subnet. Other kinds of system are set up on
   the Connection page, and *Carry on without one* is fine too.
4. **Rooms** — *Find rooms in 25Live*, then add them a building at a time or
   all at once: the same page as [Room map → From 25Live](#adding-rooms-from-25live).
5. **Schedules** — each building's schedule in the BAS (for BACnet,
   `device:instance`), checked against its system as you save. Floors, and rooms
   with schedules of their own, are on the Room map pages.
6. **Check and finish** — *Validate* and a *Dry run*, then the times the sync
   runs; *Turn the schedule on and finish*.

**Nothing reaches the BAS until you finish.** The guide creates `config.yaml`
with the schedule off. Buildings imported from 25Live wait on a `preview` system
called `staging`, which writes nothing, until the Schedules step gives each one
a real schedule. A dry run shows what they'd get. A building left staged at the
end stays that way until it has a schedule, and the guide says so.

The guide is a view over the ordinary settings files, not a store of its own,
so it can be left and picked up again, and anything it sets can be changed on
the other pages. It's under **Settings → Setup guide**. *Skip the guide* (or
*Hide this* on the status page's *Getting started* card) stops it opening; the
status page stops showing that card once the guide is finished, or the site has
run a live sync. Only whether it was finished or skipped is kept, in
`state/setup.json`.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/setup-rooms-dark.png">
  <img alt="The setup guide's Rooms step: the steps down the side, and the spaces 25Live has bookings for, grouped by building with an Add button for each, each room with its capacity, number of bookings and a building filled in from 25Live or guessed from the name" src="images/setup-rooms-light.png">
</picture>

## Adding rooms from 25Live

**Room map → From 25Live** (and the setup guide's Rooms step) lists every space
25Live lets the service account see, booked or not, grouped by building, with
each room's capacity and its number of bookings over a window you choose — 30 to
180 days. Untick *include spaces with no bookings* to list only the booked ones.
If 25Live won't list every space for the account, the page says so and shows
the booked ones. *Find rooms in 25Live* runs [`--discover`](command-line.md);
it needs *Run the tools*, and adding rooms needs *Edit the room map*.

Each room's building comes from 25Live where your instance says; otherwise it's
guessed from the name — "Science Hall 204 (Chem lab)" is in *Science Hall*, and
"Student Center Ballroom" goes with "Student Center 204". Every guess is an
editable box: change a room's building to move it to another group when you add
it.

- **One building at a time:** each building's *Add* button adds its ticked rooms,
  and nothing else. Rooms with bookings start ticked; tick the building's own
  box to take all of them, bookings or not. A building the room map doesn't have yet is created, and
  you go straight to its form to give it its schedule.
- **Or all at once:** tick rooms anywhere, and *Add the ticked rooms*.

Rooms go into the room map's building of that name (or id). A new building
waits on the `staging` preview system, which writes nothing, with a placeholder
target, until it's given its real schedule — so adding rooms never writes to a
BAS by itself. Rooms already in the map are marked *added*; a room with no
building isn't ticked until you type one. Floors, equipment and rooms' own
targets are set on the Room map pages afterwards.

## How it runs things

Every sync and tool runs as the ordinary `bas-sync` command in a process of its
own, one at a time, so the web UI can't do anything the
[command line](command-line.md) couldn't — the run lock, the safety rail and
the device checks all apply.

Saving runs the same checks the nightly sync does and asks before saving
anything it would reject. Each save is atomic, keeps a `.bak`, and is refused
if someone else changed the file since you opened the form. Rewriting a file
drops its `#` comments; for a note on a row of the room map, use its **Note**
field.

## Sign-in and roles

Out of the box there are three roles, each including the one before:

| Role | Can |
|---|---|
| **Basic** | See the status page and sync history; *Sync now* (every system). |
| **Advanced** | See everything; run the tools (dry run, validate, discover, test alert), sync one system, stop a job; add and edit rooms, buildings, floors and extra bookings. |
| **Admin** | Everything: connection, systems and passwords, alerts, defaults, schedule, safety, the files, access and appearance, the activity log, restarting the service — and *Force*, which overrides the mass-clear safety check. |

They can be changed, and more added — see [Changing the roles](#changing-the-roles).

People sign in with **Microsoft Entra ID**, and their Entra groups decide the
role. The **local password** (`BAS_WEB_PASSWORD`) can do everything, to set
that up and to get back in if it breaks. Without either, the web UI stays off
and only the schedule runs.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/signin-dark.png">
    <img alt="The sign-in page: Sign in with Microsoft, the local password, a notice and contact details" src="images/signin-light.png" width="380">
  </picture>
</p>

**Someone in several groups gets every capability of every role they're in**
(with the built-in roles, that's the highest of them). Entra lists
nested memberships in the groups claim, so a group inside another counts for
both — except with *Groups assigned to the application* (the option for very
large directories), where only groups assigned to the app directly count.

Changes on the Access page apply to people already signed in. A refused
sign-in shows there with the group values the token carried, so a mismatch
between IDs and names is easy to spot.

Access settings live in `config/web.yaml`; the client secret in the state
folder (or `BAS_WEB_SSO_CLIENT_SECRET`). If SSO breaks with the local password
off, set `local_password: true` in `web.yaml`.

## Changing the roles

The Access page shows the roles as a table: a column per role, a row per
capability. Tick what each may do and *Save roles*; rename a role in its
column's heading; *Add role* starts a new one as a copy of another. A role can
be deleted once no group has it, and *Restore the built-in roles* puts Basic,
Advanced and Admin back as they came.

| Capability | Lets someone |
|---|---|
| See the basics | See the status page and the sync history. |
| See everything | See every other page — jobs, the room map, extra bookings, settings and logs — read-only. |
| Sync now | Sync every system. |
| Run the tools | Dry run, Validate, Discover and Test alert; sync one system. |
| Stop a job | Stop a running sync or tool. |
| Force | Sync past the mass-clear safety check. |
| Edit the room map | Add and edit rooms, buildings and floors. |
| Edit extra bookings | Add and edit extra bookings. |
| Edit settings | Change the connection, systems, alerts, schedule, defaults, safety limits and the settings files; use the setup guide (adding its rooms also needs *Edit the room map*, and finding them *Run the tools*). |
| Set passwords | Set and clear the stored passwords. |
| See the activity log | See who did what. |
| Restart | Restart the service, and check for updates. |
| Manage access | Change sign-in, roles and appearance — which can grant any capability, so it's for administrators. |

What a capability needs is added when a role is saved: every page past the
status page needs *See everything*, and *Force* is a kind of sync. A role with
*Everything* ticked also gets capabilities that later versions add; Admin has
it. The built-in roles aren't written to `web.yaml` until they're changed, so
they keep picking up new capabilities too.

So nobody is locked out: the local password can always do everything, and
with it turned off, a change that would leave no group able to manage access
is refused.

## Setting up Microsoft Entra sign-in

The Access page walks through this, with the exact redirect URI to use.

1. In the Entra admin center, register an app — single tenant, redirect URI of
   type *Web*: `https://<this server>/auth/callback`. Microsoft only redirects
   to HTTPS, so the web UI needs [HTTPS](#security) first.
2. Give it a client secret, and add a **groups claim** (Token configuration →
   Add groups claim → Security groups). Emit *Group ID* and enter groups by
   object ID, or, for groups synced from on-premises AD, *sAMAccountName* and
   enter them by name.
3. On the Access page: the tenant ID, client ID and secret, then the groups —
   for example `VPN_Role_PlantOps_BAS` → Basic, `BAS_Users` → Advanced,
   `BAS_Admins` → Admin. Try it in a private window, then turn the local
   password off if you like.

Sign-in uses the OpenID Connect authorization-code flow with PKCE against your
one tenant, and checks the token's audience, issuer, tenant, expiry and nonce.
A user in more groups than a token can hold is refused, with an explanation of
the fix (*Groups assigned to the application*). Government clouds (GCC High,
DoD) are a setting on the Access page.

## Security

The web UI can start a sync that writes to building controllers, so:

- **Every page and action checks a capability.** Five wrong local passwords lock
  the address out for five minutes; changing the password, the SSO app or a
  group's role applies to open sessions. Sessions last 12 hours.
- **HTTPS**: set `BAS_WEB_TLS_CERT` and `BAS_WEB_TLS_KEY`, or put it behind a
  reverse proxy that terminates TLS and set `BAS_WEB_BEHIND_PROXY=1`. Only
  behind a proxy that does its own sign-in, `BAS_WEB_AUTH=none` turns the
  password off.
- **Where it listens**: with host networking (needed for BACnet) the web UI is
  on the host's port 8080 on every interface. Set `BAS_WEB_HOST` to one
  address, and firewall the port to the people who use it.
- Forms carry CSRF tokens; a strict Content-Security-Policy allows no inline
  script and nothing from other sites, so it also works with no internet at all.
- Every change and every job is logged with who made it and from where: in
  **Logs → Activity**, and in the service log.

## Branding

**Settings → Appearance** sets:

- a **site name**, shown in the header, the sign-in page and the browser tab;
- a **logo** — PNG, JPEG or WebP, up to 512 KB (SVG is refused, because it can
  carry script), stored beside `web.yaml`;
- an **accent colour**, with black or white text on it, whichever reads better;
- a **notice** on the sign-in page (e.g. *Authorized staff only*), and
  **contact details** — a name, an email address such as
  `plantopsbas@kennesaw.edu`, and a phone number — on the sign-in page and at
  the foot of every page.

They're saved in the `branding:` section of `web.yaml`.

## Without Docker

`pip install ".[bacnet,web]"` and run `bas-sync-service` (with
`BAS_WEB_PASSWORD` set) for the same service and web UI. The
[desktop editor](command-line.md#the-desktop-editor) is the alternative for a
site that runs the sync from Task Scheduler or cron.
