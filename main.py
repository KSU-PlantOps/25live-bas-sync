#!/usr/bin/env python3
# 25Live -> BAS Schedule Sync
# Copyright (C) 2026 Ryan Bibby and contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details <https://www.gnu.org/licenses/>.
"""
25Live -> BAS Schedule Sync — run from a source checkout.

    python main.py --validate      # pre-flight, no writes
    python main.py --dry-run       # what WOULD be written
    python main.py                 # live

The CLI itself lives in bassync/cli.py; a `pip install .` also provides it as
the `bas-sync` command. This file stays so every existing scheduled task and
cron entry that runs `python main.py` keeps working.
"""

import sys

# Oldest Python this is tested against. Anything older is either past end of
# life or close to it, which matters for a process holding service credentials
# on a building-controls network. 3.14 is what we recommend running.
#
# Checked here, before importing anything else, because the failure otherwise
# surfaces as a confusing traceback from deep inside a dependency — and this
# tends to be run by whoever is on shift, with whatever `python` is on PATH.
MIN_PYTHON = (3, 13)
RECOMMENDED_PYTHON = (3, 14)
if sys.version_info < MIN_PYTHON:
    sys.exit(
        f"25live-bas-sync needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, "
        f"but this is Python {sys.version.split()[0]} ({sys.executable}).\n"
        f"Install Python {RECOMMENDED_PYTHON[0]}.{RECOMMENDED_PYTHON[1]} (or "
        f"at least {MIN_PYTHON[0]}.{MIN_PYTHON[1]}) and make sure the scheduled "
        "task, cron entry, and Edit-Rooms.bat all invoke THAT interpreter — a "
        "common cause is dependencies installed for one Python and the job "
        "running another.")

from bassync.cli import main  # noqa: E402 — must follow the version check

if __name__ == "__main__":
    sys.exit(main())
