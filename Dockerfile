# 25Live -> BAS schedule sync — container image.
#
# This packages the headless sync. The Tkinter editor is not run in a container;
# edit space_mapping.yaml / config.yaml on a workstation, then mount them in
# (see README "Run with Docker").
#
# Build:  docker build -t 25live-bas-sync .
# Run:    docker run --rm --network host \
#           -e BAS_25LIVE_PASSWORD=... -e BAS_SYS_SUPERVISOR_PASSWORD=... \
#           -v "$(pwd)/config:/config:ro" \
#           -v bas-sync-state:/app/state \
#           25live-bas-sync --validate
#
# The state volume is not optional for live runs: it holds the previous run's
# baseline that the mass-clear safety check compares against. Without it every
# run starts with no history and only the min_events check protects you.
#
# NOTE ON BACNET: the bacnet driver binds a real NIC address and relies on
# broadcast for Who-Is, neither of which survives Docker's default bridge
# network. Run it with host networking and point `local_address` at the HOST's
# address. The niagara and rest drivers are ordinary HTTP and need none of this.

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
# campus. It is a pure-Python package, so it costs little for sites that only
# use the HTTP drivers. constraints.txt pins every version, so a rebuild
# installs exactly what was tested rather than whatever is newest that day.
COPY requirements.txt requirements-bacnet.txt constraints.txt ./
RUN pip install -r requirements.txt -r requirements-bacnet.txt -c constraints.txt

COPY main.py docker-entrypoint.sh ./
COPY bassync/ ./bassync/
RUN chmod +x docker-entrypoint.sh \
 && useradd --create-home --uid 10001 appuser \
 && mkdir -p /config /app/logs /app/state \
 && chown -R appuser:appuser /app /config

USER appuser

# Default config locations inside the image — mount your files at /config, or
# override these to point elsewhere.
ENV BAS_CONFIG=/config/config.yaml \
    BAS_DEFAULTS=/config/defaults.yaml \
    BAS_SPACE_MAP=/config/space_mapping.yaml

# The safety baseline and the run lock. A named volume here starts out owned
# by appuser (Docker copies the image's ownership into a new named volume); a
# host bind mount would be root-owned and unwritable.
VOLUME ["/app/state"]

# Args after the image name are passed straight to main.py (e.g. --validate,
# --dry-run, --discover, --test-alert). With no args, it does a live sync.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD []
