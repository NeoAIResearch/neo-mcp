"""Guard: every declared dependency must have an upper bound.

Open floors (e.g. ``mcp>=1.26.0``) can resolve to breaking majors like
mcp 2.0.0 on a fresh install. This test fails if any Python or npm
manifest reintroduces uncapped ranges.
"""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

PYTHON_ROOT = Path(__file__).resolve().parents[1]
NPM_ROOT = PYTHON_ROOT.parent / "npm"
PYPROJECT = PYTHON_ROOT / "pyproject.toml"
REQUIREMENTS = PYTHON_ROOT / "requirements.txt"
PACKAGE_JSON = NPM_ROOT / "package.json"


def _has_upper_bound(spec: SpecifierSet) -> bool:
    """True if the specifier set constrains how high a version can go."""
    if not spec:
        return False
    for sp in spec:
        if sp.operator in ("<", "<=", "~=", "==", "==="):
            return True
        # Exclusive/inclusive floors alone are not enough.
    return False


def _iter_pyproject_reqs(data: dict) -> list[tuple[str, str]]:
    """Return (source_label, requirement_string) for all declared deps."""
    out: list[tuple[str, str]] = []
    for req in data.get("project", {}).get("dependencies", []) or []:
        out.append(("project.dependencies", req))
    for extra, reqs in (data.get("project", {}).get("optional-dependencies") or {}).items():
        for req in reqs:
            out.append((f"project.optional-dependencies.{extra}", req))
    for req in data.get("build-system", {}).get("requires", []) or []:
        out.append(("build-system.requires", req))
    return out


def _parse_requirements_txt(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Drop inline comments.
        if " #" in line:
            line = line.split(" #", 1)[0].strip()
        lines.append(line)
    return lines


def _npm_range_has_upper_bound(version: str) -> bool:
    """Accept caret/tilde/exact/comparators that imply an upper bound."""
    v = version.strip()
    if not v or v == "*":
        return False
    # Exact pin or tag-like exact.
    if re.fullmatch(r"\d+\.\d+\.\d+([+-][\w.-]+)?", v):
        return True
    # Caret / tilde already major- or minor-cap.
    if v.startswith("^") or v.startswith("~"):
        return True
    # npm also allows "1.x" / "1.2.x".
    if re.fullmatch(r"\d+(\.x|\.\d+\.x)", v):
        return True
    # Comparator ranges: must include an upper bound operator somewhere.
    if "<" in v or "<=" in v:
        return True
    # Bare ">=1.0.0" / ">1.0.0" without upper → reject.
    if v.startswith(">=") or v.startswith(">"):
        return False
    # "1.2.3 - 2.0.0" hyphen ranges include an upper.
    if " - " in v:
        return True
    # Workspace / URL / file / git — not applicable here; treat as OK if present.
    if v.startswith(("file:", "git+", "http:", "https:", "workspace:")):
        return True
    return False


def _req_map(req_strings: list[str]) -> dict[str, Requirement]:
    out: dict[str, Requirement] = {}
    for raw in req_strings:
        req = Requirement(raw)
        out[req.name] = req
    return out


class DependencyBoundsTests(unittest.TestCase):
    def test_pyproject_all_deps_have_upper_bounds(self):
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        for source, raw in _iter_pyproject_reqs(data):
            with self.subTest(source=source, req=raw):
                req = Requirement(raw)
                self.assertTrue(
                    _has_upper_bound(req.specifier),
                    f"{source}: {raw!r} lacks an upper bound "
                    f"(use e.g. 'pkg>=X,<Y' — open floors can pull breaking majors)",
                )

    def test_pyproject_requires_python_has_upper_bound(self):
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        rp = data["project"]["requires-python"]
        self.assertTrue(
            _has_upper_bound(SpecifierSet(rp)),
            f"requires-python={rp!r} must have an upper bound (e.g. >=3.11,<4)",
        )

    def test_mcp_support_line(self):
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        mcp_reqs = [
            Requirement(r)
            for _, r in _iter_pyproject_reqs(data)
            if Requirement(r).name == "mcp"
        ]
        self.assertTrue(mcp_reqs, "mcp must be declared in pyproject.toml")
        for req in mcp_reqs:
            # Floor is the 1.28 support line; 2.0.0 must stay excluded.
            self.assertFalse(
                req.specifier.contains("2.0.0", prereleases=True),
                f"mcp specifier {req.specifier} must exclude 2.0.0",
            )
            self.assertFalse(
                req.specifier.contains("1.27.2", prereleases=True),
                f"mcp specifier {req.specifier} must reject pre-1.28",
            )
            self.assertTrue(
                req.specifier.contains("1.28.0", prereleases=True),
                f"mcp specifier {req.specifier} must allow 1.28.0",
            )
            self.assertTrue(
                req.specifier.contains("1.29.0", prereleases=True),
                f"mcp specifier {req.specifier} must allow 1.29.0",
            )

    def test_requirements_txt_matches_pyproject_runtime(self):
        """Docker/local installs use requirements.txt — must stay identical to pyproject."""
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        py_reqs = _req_map(list(data["project"]["dependencies"]))
        txt_reqs = _req_map(_parse_requirements_txt(REQUIREMENTS))
        self.assertEqual(
            set(py_reqs),
            set(txt_reqs),
            "requirements.txt package set must match pyproject runtime dependencies",
        )
        for name in py_reqs:
            with self.subTest(package=name):
                self.assertEqual(
                    str(py_reqs[name].specifier),
                    str(txt_reqs[name].specifier),
                    f"{name}: pyproject={py_reqs[name].specifier!s} "
                    f"requirements.txt={txt_reqs[name].specifier!s}",
                )
                self.assertTrue(_has_upper_bound(txt_reqs[name].specifier))

    def test_installed_mcp_in_support_line(self):
        """When the package is installed (CI/dev), mcp must be on the 1.28–1.x line."""
        try:
            from importlib.metadata import PackageNotFoundError, version
        except ImportError:  # pragma: no cover
            self.skipTest("importlib.metadata unavailable")
        try:
            from packaging.version import Version

            installed = Version(version("mcp"))
        except PackageNotFoundError:
            self.skipTest("mcp not installed in this environment")
        self.assertGreaterEqual(installed, Version("1.28"))
        self.assertLess(installed, Version("2"))

    def test_npm_package_json_deps_capped(self):
        self.assertTrue(PACKAGE_JSON.is_file(), f"missing {PACKAGE_JSON}")
        data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            for name, version in (data.get(section) or {}).items():
                with self.subTest(section=section, name=name, version=version):
                    self.assertTrue(
                        _npm_range_has_upper_bound(version),
                        f"npm {section}.{name}={version!r} lacks an upper bound "
                        f"(use ^ / ~ / exact / explicit <)",
                    )
        sdk = data["dependencies"]["@modelcontextprotocol/sdk"]
        self.assertTrue(
            sdk.startswith("^1."),
            f"npm MCP SDK must stay on the 1.x caret line, got {sdk!r}",
        )


if __name__ == "__main__":
    unittest.main()
