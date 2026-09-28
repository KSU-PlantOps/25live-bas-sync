[Documentation](README.md) › Running with Docker

# Running with Docker

Docker is the recommended way to run the sync: one container runs it on its
schedule and serves the [web UI](web-ui.md).

- [Quick start](#quick-start)
- [What the image runs](#what-the-image-runs)
- [BACnet needs host networking](#bacnet-needs-host-networking)
- [The config folder](#the-config-folder)
- [State, logs and the schedule](#state-logs-and-the-schedule)
- [Restarting](#restarting)
- [Using a published image](#using-a-published-image)
- [Updating](#updating)
- [Updating automatically](#updating-automatically)
- [Environment variables](#environment-variables)

## Quick start

You need a Linux host or VM with Docker and the compose plugin, that can reach
25Live over HTTPS and your BAS over BACnet/IP (UDP 47808). If it isn't on the
controls network, see
[Running in a datacenter or the cloud](networking.md#running-in-a-datacenter-or-the-cloud).

```bash
git clone https://github.com/KSU-PlantOps/25live-bas-sync.git
cd 25live-bas-sync
cp .env.example .env        # set BAS_WEB_PASSWORD
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

Passwords can be set on the web UI (Connection and Alerts pages), or in `.env`
— which wins — followed by `docker compose up -d` to restart with them. The web
UI shows which are missing and where each one comes from, never the value.

The settings you make in the web UI are ordinary YAML files in `./config`, so
you can back them up, diff them, or keep them in git.

## What the image runs

The image runs in one of two ways (see `docker-entrypoint.sh`):

- **`serve`** — the service: the sync on its schedule plus the web UI. This is
  what `docker-compose.yml` runs.
- **`sync [args]`** — one run of the [command line](command-line.md), then
  exit with its code: `sync --validate`, `sync --dry-run`, or plain `sync` for
  a live run. Use it for host cron or a **Kubernetes CronJob**, or for a
  one-off next to the service:

  ```bash
  docker compose run --rm sync sync --validate
  docker compose run --rm sync sync --dry-run
  ```

  1.x-style flags straight after the image name (`--validate`) still work, and
  with no arguments the image does one live sync — or runs the service if
  `SYNC_AT` is set — as 1.x did.

## BACnet needs host networking

> [!IMPORTANT]
> The BACnet driver binds a real NIC address and relies on broadcast, neither
> of which survives Docker's default bridge. Run it with `network_mode: host`
> (already set in the compose file) and set `local_address` to the *host's*
> address.

On Docker Desktop for Mac/Windows, host networking is limited — run it on a
Linux host or VM. The `rest` driver is ordinary HTTP and works under bridge
networking: drop `network_mode` and publish the port (`ports: ["8080:8080"]`).

With host networking the web UI is on the host's port 8080 on every interface.
Set `BAS_WEB_HOST` to one address, and firewall the port to the people who use
it — see [Security](web-ui.md#security).

## The config folder

`./config` is mounted read-write at `/config`: the web UI saves `config.yaml`,
`defaults.yaml`, `space_mapping.yaml`, `extra_bookings.yaml` and `web.yaml`
there (and an uploaded logo). The container starts as root only long enough to sort out file
ownership, then runs as **whoever owns that folder on the host**, so the files
stay yours and you can still edit them there.

- Set `PUID`/`PGID` in `.env` to run as someone else.
- A folder Docker created itself (root-owned and empty) is handed to the
  image's own user, 10001.
- If the web UI says it can't save, the folder isn't writable by the user it
  runs as: `sudo chown -R "$(id -u):$(id -g)" config` fixes it.
- A read-only mount (`:ro`) also works — the web UI then shows the settings
  but can't change them.

The container points at `/config/*.yaml` through the `BAS_CONFIG`,
`BAS_DEFAULTS` and `BAS_SPACE_MAP` environment variables.

## State, logs and the schedule

> [!WARNING]
> **Keep the `state` volume.** It holds the previous run's baseline that the
> [mass-clear safety check](safety.md) compares against, the run history, the
> job output, the passwords set on the web UI and the run lock. Compose mounts
> a named volume for it; with `docker run` or a CronJob, mount a named volume
> or PVC at `/app/state` yourself. Without it every run starts with no history
> — the sync warns about that on every run, and the entrypoint warns if
> `/app/state` isn't writable.

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

Passwords are never baked into the image. They come from the environment
(`.env`), or from the web UI, which keeps them in the state volume
(`state/secrets.json`, readable only by the service).

## Restarting

**Settings → Service → Restart the service** (the *restart* capability — Admin
by default) stops the service and starts it again in place, in a few
seconds, with nobody signed out. Settings made on the web UI apply without it;
restart to load a renewed HTTPS certificate, after changing where logs or
state are kept, or if something seems stuck. It isn't offered while a job is
running or a scheduled sync is minutes away.

It doesn't re-read the container's environment. After changing `.env`, run
`docker compose up -d` on the host, which recreates the container with the
new values; `docker compose restart` keeps the old ones.

## Using a published image

Each [release](https://github.com/KSU-PlantOps/25live-bas-sync/releases) is
published to GHCR for amd64 and arm64:

| Tag | Follows |
|---|---|
| `ghcr.io/ksu-plantops/25live-bas-sync:1.3` | the 1.3 line, including its patch releases — recommended |
| `ghcr.io/ksu-plantops/25live-bas-sync:1.3.0` | exactly one release |
| `ghcr.io/ksu-plantops/25live-bas-sync:latest` | the newest release |

A release candidate is marked as a pre-release and has only its exact tag,
e.g. `:1.3.0rc1`; `:1.3` and `:latest` never move to one.

To run a published release rather than build locally, delete `build: .` from
`docker-compose.yml` and set `image:` to one of those.

## Updating

Read [Upgrading](upgrading.md) for anything a release asks you to do, then:

```bash
# Built from the repository:
git pull
docker compose up -d --build

# From a published image:
docker compose pull
docker compose up -d
```

The config folder and the volumes carry over. **Settings → Service** says
when a newer release is out: the service asks GitHub's releases API twice a
day (turn it off there if the host shouldn't reach GitHub), and Admins see a
note on the status page.

## Updating automatically

The container can't update itself: replacing its own image would need the
Docker socket mounted inside it, which is root on the host — a poor trade for
a service on a controls network. The host can do it on a timer instead.

With a published image on a **minor-version tag** (`:1.3`), a timer that runs
`docker compose pull` and `docker compose up -d` picks up each patch release —
fixes only, no settings to change — and recreates the container only when the
image has changed. A sync that is writing gets to finish first. Minor and
major releases (`1.4`, `2.0`) stay a deliberate change of tag, after reading
[Upgrading](upgrading.md); don't point a timer at `:latest`.

`contrib/systemd/` has the units, for Linux hosts with systemd:

```bash
sudo cp contrib/systemd/25live-bas-sync-update.* /etc/systemd/system/
# If docker-compose.yml isn't in /opt/25live-bas-sync, set WorkingDirectory=:
sudo systemctl edit 25live-bas-sync-update.service
sudo systemctl enable --now 25live-bas-sync-update.timer
systemctl list-timers 25live-bas-sync-update.timer     # when it runs next
journalctl -u 25live-bas-sync-update.service            # what it did
```

The timer runs mid-morning on Tuesdays, away from the nightly sync and when
someone is around to notice; change `OnCalendar=` in the timer to suit.
Without systemd, the same two commands from cron do the job.

## Environment variables

Set these in `.env`; every variable in it is passed into the container.
`.env.example` has them all, commented.

| Variable | What it does |
|---|---|
| `BAS_WEB_PASSWORD` | The local password, which signs in as Admin. Without it, and without single sign-on, the web UI stays off and only the schedule runs. |
| `BAS_WEB_HOST` · `BAS_WEB_PORT` | Where the web UI listens (default `0.0.0.0`, `8080`). |
| `BAS_WEB_TLS_CERT` · `BAS_WEB_TLS_KEY` | Serve HTTPS with this certificate (PEM, full chain) and key. |
| `BAS_WEB_BEHIND_PROXY=1` | Trust `X-Forwarded-For`/`-Proto` from one reverse proxy that terminates HTTPS. |
| `BAS_WEB_AUTH=none` | No sign-in at all — only behind a proxy that does its own. |
| `BAS_WEB_SSO_CLIENT_SECRET` | The Entra app's client secret, instead of setting it on the Access page. |
| `BAS_25LIVE_PASSWORD` | The 25Live service account's password. |
| `BAS_SYS_<NAME>_PASSWORD` | One per BAS system that logs in (the `rest` driver) — see [Secrets](configuration.md#secrets). |
| `BAS_SMTP_PASSWORD` | The alert email account's password. |
| `BAS_ALERT_WEBHOOK_URL` | The alert webhook, instead of keeping it in `config.yaml`. |
| `BAS_EXTRA_BOOKINGS` | Where the extra bookings are, if not `extra_bookings.yaml` beside `config.yaml`. |
| `TZ` | The container's clock and log timestamps. The sync and its schedule use `timezone:` in `config.yaml`. |
| `PUID` · `PGID` | Run as this user and group instead of the config folder's owner. |
| `SYNC_AT` · `SYNC_ON_START=1` | 1.x settings, still honoured: override the schedule's times with one `HH:MM`, and run once at start. |
| `BAS_STOP_GRACE` | Seconds a running sync gets to finish when the container stops (default 90). |

The passwords can instead be set on the web UI; one set in the environment
always wins.
