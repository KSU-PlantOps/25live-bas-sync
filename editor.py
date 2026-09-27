#!/usr/bin/env python3
# 25Live -> BAS Schedule Sync — Room Mapping Editor launcher
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Launch the Room Mapping Editor from a source checkout:

    python editor.py                 # opens space_mapping.yaml next to this file
    python editor.py path\\to\\map.yaml

The editor itself lives in bassync/editor.py; a `pip install .` also provides
it as the `bas-sync-editor` command. Edit-Rooms.bat runs this file.
"""

import sys

if sys.version_info < (3, 13):
    sys.exit("The Room Mapping Editor needs Python 3.13 or newer "
             f"(this is {sys.version.split()[0]}, {sys.executable}).")

from bassync.editor import main  # noqa: E402 — must follow the version check

if __name__ == "__main__":
    raise SystemExit(main())
