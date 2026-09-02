"""Start the tray application without depending on the working directory.

The logon entry points here so it can be a single pythonw.exe invocation:
a .pyw file opens no console, and prepending this directory to the path
makes the package importable wherever Windows happens to start it.
"""

import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

runpy.run_module("tvtest_epg_runner", run_name="__main__")
