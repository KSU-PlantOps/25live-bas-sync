# 25Live -> BAS schedule sync — container image.
#
# The image runs the service: the sync on its schedule, plus the web UI for
# status, "Sync now" and editing the room map and settings. docker compose is
# the way to run it (see docker-compose.yml and docs/docker.md):
#
#   docker compose up -d            # then browse to http://<host>:8080
#
# Or by hand:
#
#   docker run -d --network host --init \
#     --env-file .env \
#     -v "$(pwd)/config:/config" \
#     -v bas-sync-state:/app/state \
#     ghcr.io/ksu-plantops/25live-bas-sync:1.4 serve
#
# `sync --validate`, `sync --dry-run` or plain `sync` instead of `serve` runs
# once and exits (host cron, a Kubernetes CronJob). See docker-entrypoint.sh.
#
# The state volume is not optional: it holds the previous run's baseline that
# the mass-clear safety check compares against, the run history and the run
# lock. Without it every run starts with no history.
#
# NOTE ON BACNET: the bacnet driver binds a real NIC address and relies on
# broadcast for Who-Is, neither of which survives Docker's default bridge
# network. Run it with host networking and point `local_address` at the HOST's
# address. The rest driver is ordinary HTTP and needs none of this.

# Matches the recommended runtime rather than the 3.13 floor.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: zoneinfo needs a zone database (mirrors the requirements.txt note for
# Windows; the OS package keeps it current). ca-certificates: HTTPS to 25Live.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# BACpypes3 is included because bacnet is the recommended driver for a mixed
# campus; Flask and cheroot are the web UI. All pure Python. constraints.txt
# pins every version, so a rebuild installs exactly what was tested rather
# than whatever is newest that day.
COPY requirements.txt requirements-bacnet.txt requirements-web.txt constraints.txt ./
RUN pip install -r requirements.txt -r requirements-bacnet.txt \
        -r requirements-web.txt -c constraints.txt

COPY main.py docker-entrypoint.sh ./
COPY bassync/ ./bassync/
RUN chmod +x docker-entrypoint.sh \
 && useradd --create-home --uid 10001 appuser \
 && mkdir -p /config /app/logs /app/state \
 && chown -R appuser:appuser /app/logs /app/state /config

# Default config locations inside the image — mount your folder at /config, or
# override these to point elsewhere.
ENV BAS_CONFIG=/config/config.yaml \
    BAS_DEFAULTS=/config/defaults.yaml \
    BAS_SPACE_MAP=/config/space_mapping.yaml

# The safety baseline, run history and run lock. A named volume here starts
# out owned by appuser; the entrypoint hands it to whichever user it runs as.
VOLUME ["/app/state"]

# The web UI (BAS_WEB_PORT). With host networking, it is the host's port.
EXPOSE 8080

LABEL org.opencontainers.image.title="25live-bas-sync" \
      org.opencontainers.image.description="Drive building automation occupancy schedules from 25Live room bookings" \
      org.opencontainers.image.source="https://github.com/KSU-PlantOps/25live-bas-sync" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# No USER line on purpose: the entrypoint starts as root only to give the
# mounted /config folder and the volumes to the right user, then drops to that
# user (setpriv) before running anything. See docker-entrypoint.sh.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD []
