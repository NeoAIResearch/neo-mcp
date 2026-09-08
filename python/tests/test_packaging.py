"""Source-package contract tests; wheel installation is verified separately."""

from __future__ import annotations

import tomllib
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_console_entrypoint_targets_server_main() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["neo-mcp"] == "neo_mcp.server:main"


def test_module_entrypoint_and_runtime_modules_exist() -> None:
    assert find_spec("neo_mcp") is not None
    assert find_spec("neo_mcp.__main__") is not None
    for module in (
        "action_handlers.py",
        "backend_client.py",
        "backend_poller.py",
        "ipc_state.py",
        "job_manager.py",
        "server.py",
    ):
        assert (ROOT / "src" / "neo_mcp" / module).is_file()


def test_bundled_resources_declared_and_present() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = data["tool"]["setuptools"]["package-data"]["neo_mcp"]
    assert "skills/*.md" in package_data
    assert "postman/*.md" in package_data
    assert (ROOT / "src" / "neo_mcp" / "skills" / "neo.md").is_file()
    assert len(list((ROOT / "src" / "neo_mcp" / "postman").glob("*.md"))) >= 5
