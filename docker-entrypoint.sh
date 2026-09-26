#!/bin/sh
# Entrypoint for the 25Live -> BAS sync container.
#
# Default (SYNC_AT unset): a ONE-SHOT run. Whatever args were passed after the
# image name go straight to main.py (e.g. --validate / --dry-run / --discover),
# then the container exits with main.py's exit code. Schedule it however you
# already schedule jobs — host cron, `docker compose run`, or a k8s CronJob.
#
# Optional built-in scheduler (SYNC_AT=HH:MM, 24-hour, in the container's TZ):
# the container stays up and runs the sync once per day at that time. Set
# SYNC_ON_START=1 to also run once immediately on startup. This makes
# `docker compose up -d` a self-contained nightly sync with no external cron.
# The scheduling itself is bassync/scheduler.py, which handles the DST
# changeover (02:00 does not exist on the spring-forward date).
set -eu

# The safety rail compares each run with the previous one, using a small state
# file in /app/state. If that directory is not writable — or not a volume, so
# it vanishes with the container — the comparison silently has nothing to
# compare against. Say so loudly, every start.
if ! ( : > /app/state/.write-test ) 2>/dev/null; then
    echo "[entrypoint] WARNING: /app/state is not writable by uid $(id -u)." \
         "The mass-clear safety check will have no baseline. Mount a named" \
         "volume there (docker compose does), not a root-owned host folder." >&2
else
    rm -f /app/state/.write-test
fi

# One-shot mode — the idiomatic container default.
if [ -z "${SYNC_AT:-}" ]; then
    exec python main.py "$@"
fi

if [ "${SYNC_ON_START:-0}" = "1" ]; then
    exec python -m bassync.scheduler "$SYNC_AT" --on-start -- "$@"
fi
exec python -m bassync.scheduler "$SYNC_AT" -- "$@"
