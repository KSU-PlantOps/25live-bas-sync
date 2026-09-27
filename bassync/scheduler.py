# 25Live -> BAS Schedule Sync — built-in daily scheduler (Docker)
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Run the sync once a day at a fixed local time, forever.

    python -m bassync.scheduler 02:00 [--on-start] [-- main.py args...]

This is what `docker compose up -d` runs when SYNC_AT is set (see
docker-entrypoint.sh). The time is in the container's TZ.

It exists because the shell loop it replaced asked GNU `date` for "tomorrow
02:00" — and on the spring-forward date in US timezones 02:00 does not exist.
`date` failed, the entrypoint exited, and `restart: unless-stopped` turned
that into a crash loop that skipped the night's sync. Here a nonexistent time
runs at the same instant the clock jumps to (02:00 -> 03:00), and a repeated
one (the fall-back hour) runs once, at its first occurrence.

A failed sync never stops the scheduler: the sync has already logged and
alerted, and tomorrow is another run.
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_AT_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_at(text: str) -> tuple:
    """'02:00' -> (2, 0). Raises ValueError for anything that isn't a real
    24-hour time — '29:00' used to pass the old shell regex."""
    m = _AT_RE.match(text or "")
    if not m:
        raise ValueError(f"{text!r} is not a 24-hour HH:MM time")
    return int(m.group(1)), int(m.group(2))


def next_run(at: str, now: datetime) -> datetime:
    """
    The next instant (timezone-aware, in `now`'s zone) whose local wall-clock
    time is `at`, strictly after `now`.

    zoneinfo resolves a wall time that falls in a spring-forward gap using the
    offset from before the jump, which lands on the real instant the clock
    jumps to; converting through UTC makes that explicit. For a repeated
    fall-back time, fold=0 picks the first occurrence.

    Every comparison is made in UTC: Python compares two datetimes that share
    a tzinfo by wall-clock time and ignores `fold`, so inside the repeated
    hour the already-past first 01:30 would otherwise look like the future.
    """
    tz = now.tzinfo
    now_utc = now.astimezone(timezone.utc)
    hour, minute = parse_at(at)
    for days in range(0, 3):
        day = (now + timedelta(days=days)).date()
        wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        instant_utc = wall.astimezone(timezone.utc)
        if instant_utc > now_utc:
            return instant_utc.astimezone(tz)
    raise RuntimeError("no next run found")            # pragma: no cover


def next_run_any(times, now: datetime) -> Optional[datetime]:
    """The soonest of next_run() over several HH:MM times; None for none."""
    runs = [next_run(at, now) for at in times]
    return min(runs, key=lambda d: d.astimezone(timezone.utc)) if runs else None


def _zone() -> ZoneInfo:
    name = os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:                                   # noqa: BLE001
        logging.warning("[scheduler] TZ=%r is not a known timezone; using UTC", name)
        return ZoneInfo("UTC")


def _run_sync(args: list) -> int:
    main_py = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")
    command = ([sys.executable, main_py] if os.path.isfile(main_py)
               else [sys.executable, "-m", "bassync.cli"]) + list(args)
    code = subprocess.call(command)
    if code:
        logging.warning("[scheduler] sync exited %d (it has already logged and "
                        "alerted); next run as scheduled", code)
    return code


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bassync.scheduler",
        description="Run the sync every day at a fixed local time (container TZ).")
    parser.add_argument("at", help="24-hour HH:MM, e.g. 02:00")
    parser.add_argument("--on-start", action="store_true",
                        help="also run once immediately")
    # Everything after `--` belongs to the sync, not to us. Split by hand:
    # argparse.REMAINDER would also swallow --on-start.
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        split = argv.index("--")
        argv, sync_args = argv[:split], argv[split + 1:]
    else:
        sync_args = []
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        stream=sys.stdout)
    try:
        parse_at(args.at)
    except ValueError as exc:
        logging.error("[scheduler] SYNC_AT %s. Exiting.", exc)
        return 2
    tz = _zone()
    logging.info("[scheduler] running the sync daily at %s (%s)%s", args.at, tz.key,
                 f" with {' '.join(sync_args)}" if sync_args else "")
    if args.on_start:
        logging.info("[scheduler] running once on start")
        _run_sync(sync_args)
    while True:
        now = datetime.now(tz)
        target = next_run(args.at, now)
        # Subtract in UTC; same-tzinfo subtraction is wall-clock arithmetic and
        # is off by an hour across a DST change.
        wait = max((target.astimezone(timezone.utc)
                    - now.astimezone(timezone.utc)).total_seconds(), 1)
        logging.info("[scheduler] next run %s (in %ds)",
                     target.strftime("%Y-%m-%d %H:%M %Z"), int(wait))
        time.sleep(wait)
        _run_sync(sync_args)


if __name__ == "__main__":
    sys.exit(main())
