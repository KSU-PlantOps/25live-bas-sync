#!/bin/sh
# Entrypoint for the 25Live -> BAS sync container.
#
#   serve                the service: the sync on its schedule (config.yaml's
#                        `schedule:`, editable in the web UI) plus the web UI.
#                        What docker-compose.yml runs.
#   sync [args]          one run of the command line, then exit with its code:
#                        `sync --validate`, `sync --dry-run`, or plain `sync`
#                        for a live run (host cron, a Kubernetes CronJob).
#   --flag ...           the same, 1.x style: `--validate`, `--dry-run`, ...
#   (nothing)            1.x compatible: with SYNC_AT set, `serve`; without
#                        it, one live sync.
#   anything else        run as given, e.g. `sh` for a look around.
#
# Who it runs as: the container starts as root only to sort out file
# ownership, then drops to an ordinary user for everything else:
#   - PUID / PGID when set;
#   - otherwise whoever owns the mounted /config folder, so settings saved
#     from the web UI stay owned by the person who owns the folder on the
#     host (and they can still edit them there);
#   - otherwise the image's own user, 10001.
# The state and logs volumes are handed to that user. Started as a non-root
# user already (Kubernetes runAsUser, `user:` in compose), it skips all of
# this and runs as it is.
set -eu

if [ "$(id -u)" = 0 ]; then
    uid=${PUID:-}
    gid=${PGID:-}
    owner=$(stat -c %u /config 2>/dev/null || echo 0)
    if [ -z "$uid" ] && [ "$owner" != 0 ]; then
        uid=$owner
        gid=${gid:-$(stat -c %g /config)}
    fi
    uid=${uid:-10001}
    gid=${gid:-$uid}
    # A folder Docker made for a missing bind mount is root's and empty.
    if [ "$owner" = 0 ] && [ -z "$(ls -A /config 2>/dev/null)" ]; then
        chown "$uid:$gid" /config 2>/dev/null || true
    fi
    for dir in /app/state /app/logs; do
        if [ -d "$dir" ] && [ "$(stat -c %u "$dir")" != "$uid" ]; then
            chown -R "$uid:$gid" "$dir" 2>/dev/null || true
        fi
    done
    export HOME=/tmp
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --inh-caps=-all \
        -- "$0" "$@"
fi

# The safety rail compares each run with the previous one, using a small state
# file in /app/state. If that directory is not writable — or not a volume, so
# it vanishes with the container — the comparison silently has nothing to
# compare against. Say so loudly, every start.
if ! ( : > /app/state/.write-test ) 2>/dev/null; then
    echo "[entrypoint] WARNING: /app/state is not writable by uid $(id -u)." \
         "The mass-clear safety check will have no baseline. Mount a named" \
         "volume there (docker compose does), not a host folder you can't write." >&2
else
    rm -f /app/state/.write-test
fi

mode=once
case "${1:-}" in
    serve) shift; mode=serve ;;
    sync) shift ;;
    -*) ;;
    "") [ -n "${SYNC_AT:-}" ] && mode=serve ;;
    *) exec "$@" ;;
esac

if [ "$mode" = serve ]; then
    if [ ! -w /config ]; then
        echo "[entrypoint] NOTE: /config is not writable by uid $(id -u), so the" \
             "web UI can show the settings but not save them. Remove :ro from" \
             "the mount, or set PUID/PGID to the folder's owner." >&2
    fi
    exec python -m bassync.service "$@"
fi
exec python main.py "$@"
