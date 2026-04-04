"""Tests for neo_mcp.auth deployment ID selection policy."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from neo_mcp.auth import derive_deployment_id, get_or_create_deployment_id


class TestAuthDeploymentIdPolicy(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.mkdtemp()
        self._orig_home = os.environ.get("HOME")
        self._orig_dep = os.environ.get("NEO_DEPLOYMENT_ID")
        self._orig_mode = os.environ.get("NEO_DEPLOYMENT_ID_MODE")
        os.environ["HOME"] = self._td
        os.environ.pop("NEO_DEPLOYMENT_ID", None)
        os.environ.pop("NEO_DEPLOYMENT_ID_MODE", None)

    def tearDown(self):
        if self._orig_home is not None:
            os.environ["HOME"] = self._orig_home
        else:
            os.environ.pop("HOME", None)
        if self._orig_dep is not None:
            os.environ["NEO_DEPLOYMENT_ID"] = self._orig_dep
        else:
            os.environ.pop("NEO_DEPLOYMENT_ID", None)
        if self._orig_mode is not None:
            os.environ["NEO_DEPLOYMENT_ID_MODE"] = self._orig_mode
        else:
            os.environ.pop("NEO_DEPLOYMENT_ID_MODE", None)

    def test_explicit_override_has_highest_priority(self):
        os.environ["NEO_DEPLOYMENT_ID"] = "explicit-123"
        os.environ["NEO_DEPLOYMENT_ID_MODE"] = "key-derived"
        uid = get_or_create_deployment_id("sk-v1-test")
        self.assertEqual(uid, "explicit-123")

    def test_key_derived_mode_matches_helper(self):
        os.environ["NEO_DEPLOYMENT_ID_MODE"] = "key-derived"
        uid = get_or_create_deployment_id("sk-v1-test")
        self.assertEqual(uid, derive_deployment_id("sk-v1-test"))

    def test_key_derived_mode_without_key_falls_back_to_machine_uuid(self):
        os.environ["NEO_DEPLOYMENT_ID_MODE"] = "key-derived"
        uid = get_or_create_deployment_id("")
        self.assertRegex(uid, r"^[a-f0-9\-]{36}$")
        # verify persistence file is still written in fallback mode
        standalone = Path(self._td) / ".neo" / "daemon" / "standalone_deployment_id"
        self.assertTrue(standalone.exists())
        self.assertEqual(uid, standalone.read_text().strip())

    def test_default_mode_persists_same_uuid_even_if_key_changes(self):
        uid1 = get_or_create_deployment_id("sk-v1-first")
        uid2 = get_or_create_deployment_id("sk-v1-second")
        self.assertEqual(uid1, uid2)

    def test_reads_existing_standalone_uuid_with_whitespace(self):
        daemon_dir = Path(self._td) / ".neo" / "daemon"
        daemon_dir.mkdir(parents=True)
        (daemon_dir / "standalone_deployment_id").write_text("  persisted-uuid  \n")
        uid = get_or_create_deployment_id("sk-v1-test")
        self.assertEqual(uid, "persisted-uuid")


if __name__ == "__main__":
    unittest.main()
