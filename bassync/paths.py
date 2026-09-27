# 25Live -> BAS Schedule Sync — where files live
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
One answer to "where are config.yaml, the logs and the safety state?", shared
by the CLI, the editor and the Docker image.

    $BAS_HOME          wins when set — point a pip-installed copy at a folder
    source checkout    the folder holding main.py, as it always has been
    otherwise          the current working directory

The checkout rule keeps every existing Task Scheduler / cron deployment
working unchanged. The fallback is what a `pip install`-ed `bas-sync` needs,
since the package directory then lives inside site-packages.
"""

import os
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent


def base_dir() -> Path:
    env = os.environ.get("BAS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    checkout = _PACKAGE_DIR.parent
    if (checkout / "main.py").is_file():
        return checkout
    return Path.cwd()


def config_file() -> Path:
    return base_dir() / "config.yaml"


def defaults_file() -> Path:
    return base_dir() / "defaults.yaml"


def space_map_file() -> Path:
    return base_dir() / "space_mapping.yaml"


def log_file() -> Path:
    return base_dir() / "logs" / "25live_sync.log"


def state_dir() -> Path:
    """
    The safety rail's memory, and the run lock.

    Deliberately NOT under logs/: people clean log folders out, and Docker
    users often skip mounting them. Losing this directory silently disables
    the mass-clear comparison, so it gets a home of its own.
    """
    return base_dir() / "state"


def state_file() -> Path:
    return state_dir() / "last_run.json"


def legacy_state_file() -> Path:
    """Where 1.x kept the state. Read once as a fallback so an upgrade keeps
    its baseline instead of starting from "no history"."""
    return base_dir() / "logs" / "last_run.json"
