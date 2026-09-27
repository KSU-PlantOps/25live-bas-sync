# 25Live -> BAS Schedule Sync — single-run lock
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
One live sync at a time.

Two overlapping runs — the nightly task plus someone re-running by hand, or a
slow run still going when the next one fires — would both write every
schedule and race on the safety state file. The second run takes the lock or
exits straight away (exit code 8) without touching anything.

The lock is an OS file lock (fcntl on Linux/macOS, msvcrt on Windows), so it
is released automatically if the process dies — there is no stale lock file
to clean up after a crash.
"""

import logging
import os
import sys
from pathlib import Path
from typing import IO, Optional


class RunLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fh: Optional[IO] = None

    def acquire(self) -> bool:
        """True if this process now holds the lock. False if another run
        does. Raises OSError if the lock file can't be created at all."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logging.debug("Releasing run lock: %s", exc)
        finally:
            self._fh.close()
            self._fh = None

    def holder(self) -> str:
        """The PID recorded by whoever holds the lock, for the message."""
        try:
            return self.path.read_text(encoding="utf-8").strip() or "?"
        except OSError:
            return "?"
