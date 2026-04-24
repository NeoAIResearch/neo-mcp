"""Pytest bootstrap — isolates tests from the user's real ~/.neo/.

Must run before any `neo_mcp` module is imported, because paths.py evaluates
NEO_HOME at import time (module-level constants). Pytest loads conftest.py
before collecting test files, so setting the env var here redirects every
subsequent disk write (daemon.log, thread-workspaces.json, integrations/)
into a throwaway tmp dir unique to this test run.
"""

import os
import tempfile

os.environ.setdefault("NEO_HOME", tempfile.mkdtemp(prefix="neo-test-home-"))
