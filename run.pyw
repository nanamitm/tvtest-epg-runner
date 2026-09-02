"""Start the tray application without depending on the working directory.

The logon entry points here so it can be a single command: a .pyw file opens
no console, and Python puts this directory on the path, so the package is
importable wherever Windows happens to start it.

The import is written out rather than resolved through runpy so that a
packaging tool can see the package and carry it along.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tvtest_epg_runner.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
