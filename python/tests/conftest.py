"""Pytest bootstrap — isolates tests from the user's real ~/.neo/.

Must run before any `neo_mcp` module is imported, because paths.py evaluates
NEO_HOME at import time (module-level constants). Pytest loads conftest.py
before collecting test files, so setting the env var here redirects every
subsequent disk write (daemon.log, thread-workspaces.json, integrations/)
into a throwaway tmp dir unique to this test run.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if "NEO_HOME" not in os.environ:
    _test_home = tempfile.mkdtemp(prefix="neo-test-home-")
    os.environ["NEO_HOME"] = _test_home
    atexit.register(shutil.rmtree, _test_home, ignore_errors=True)

# Always test the checkout, never an unrelated globally installed neo-mcp.
src = str(Path(__file__).resolve().parents[1] / "src")
if src not in sys.path:
    sys.path.insert(0, src)
