#!/usr/bin/env python3
"""
Offline tests — no 25Live, no BAS, no network.

The suite lives in tests/ and runs under pytest. This file is kept so the
long-documented `python Test.py` still works:

    python Test.py              # everything
    python Test.py -k bacnet    # any pytest arguments pass straight through

Install the test tools first:  pip install -r requirements-dev.txt
"""

import sys

try:
    import pytest
except ImportError:
    sys.exit("The test suite runs under pytest: pip install -r requirements-dev.txt")

if __name__ == "__main__":
    sys.exit(pytest.main(["tests", *sys.argv[1:]]))
