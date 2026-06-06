# Publishing neo-mcp

Release artifacts for the Python MCP server, version **0.5.4**. Everything below is
prepared and validated; the steps that actually upload require your own credentials.

| Target | Source of truth | Status |
|---|---|---|
| PyPI package | `python/pyproject.toml` | ready to build & upload |
| `.mcpb` bundle (Claude Desktop / Anthropic) | `mcpb/manifest.json` + `mcpb/build.sh` | build with `./mcpb/build.sh` |
| Official MCP registry | `.mcp/server.json` | ready (needs PyPI 0.5.4 live first) |
| Smithery | `smithery.yaml` | ready |

Keep all versions in lockstep: `pyproject.toml`, `.mcp/server.json` (×2:
top-level + `packages[].version`), and `mcpb/manifest.json`.

The published GitHub identity is **`NeoAIResearch/neo-mcp`** (matches the git
remote), and the MCP-registry name is **`io.github.NeoAIResearch/neo-mcp`**.

---

## 1. PyPI (`pip install neo-mcp`)

The registry and Smithery both resolve the live PyPI package, so publish this first.
0.5.3 is already on PyPI, so 0.5.4 is the next valid upload.

```bash
cd python
python3 -m pip install --upgrade build twine
python3 -m build                 # -> dist/neo_mcp-0.5.4-py3-none-any.whl + .tar.gz
python3 -m twine check dist/*
python3 -m twine upload dist/*   # needs your PyPI API token (~/.pypirc or TWINE_PASSWORD)
```

The package README contains the ownership token
`mcp-name: io.github.NeoAIResearch/neo-mcp` (top of `python/README.md`) — required
for MCP-registry verification. Don't remove it.

## 2. `.mcpb` bundle (Anthropic directory / Claude Desktop)

```bash
./mcpb/build.sh                  # -> mcpb/dist/neo-mcp-0.5.4.mcpb
npx @anthropic-ai/mcpb validate mcpb/manifest.json
```

Install locally to test: open `mcpb/dist/neo-mcp-0.5.4.mcpb` in Claude Desktop
(Settings → Extensions), or drag-and-drop it.

**Runtime:** the bundle uses the **uv runtime** (`manifest_version` 0.4,
`server.type: "uv"`). It ships only the `neo_mcp` source + `pyproject.toml` — no
vendored wheels. At install/first-launch, Claude Desktop's bundled `uv` fetches a
matching Python (>=3.11) and installs the correct per-platform dependency wheels.

- ✅ **One `.mcpb` works on macOS, Windows, and Linux** — no per-OS CI build needed.
- ℹ️ **First launch needs network** (to resolve deps), like any `pip`/`uv` install.
  Subsequent launches use uv's cache and start offline.
- The user must set **Workspace Directory** (required) — it maps to `NEO_WORKSPACE_DIR`,
  the project root Neo reads/writes. Without uv there is no project cwd, so this is
  mandatory rather than defaulted.

## 3. Official MCP registry (registry.modelcontextprotocol.io)

Uses `.mcp/server.json` (validated against schema `2025-12-11`, name
`io.github.NeoAIResearch/neo-mcp`).

**Automated (recommended):** `.github/workflows/publish-registry.yml` runs on every
`v*` tag. It waits for the matching PyPI release, then authenticates with **GitHub
OIDC** (no secrets) and publishes — valid because the repo lives under the
`NeoAIResearch` org that owns the `io.github.NeoAIResearch/*` namespace. Just push
the tag (step 6).

**Manual:** PyPI 0.5.4 must be live first (step 1).

```bash
brew install mcp-publisher                 # or download from the registry repo releases
mcp-publisher login github                 # device-code OAuth — must own github.com/NeoAIResearch
mcp-publisher publish .mcp/server.json     # explicit path (CLI defaults to ./server.json)
```

The registry downloads the PyPI package and checks its README contains
`mcp-name: io.github.NeoAIResearch/neo-mcp` — already in place.

## 4. Smithery (smithery.ai)

Uses `smithery.yaml` (stdio install via `uvx neo-mcp`). Connect the GitHub repo at
https://smithery.ai/new and it picks up `smithery.yaml`. Users supply `neoSecretKey`
(+ optional environment / workspace) through Smithery's config UI.

---

## Release checklist

1. Bump version in `python/pyproject.toml`, `.mcp/server.json` (both spots),
   `mcpb/manifest.json`, and the `CLAUDE.md` version line.
2. `cd python && python3 -m pytest tests/test_system.py`.
3. `./mcpb/build.sh` → `npx @anthropic-ai/mcpb validate mcpb/manifest.json`; attach
   `mcpb/dist/neo-mcp-<version>.mcpb` to the GitHub release.
4. Push the tag: `git tag v0.5.4 && git push --tags`. This triggers, in order:
   - `publish-pypi.yml` → builds + uploads to PyPI (needs `PYPI_API_TOKEN` secret)
   - `publish-registry.yml` → waits for PyPI, then publishes to the MCP registry via GitHub OIDC
   - `publish-npm.yml` (npm daemon, on `npm-v*` tags)
5. Connect the repo on Smithery (one-time) — it reads `smithery.yaml` automatically.

> Required repo secret: `PYPI_API_TOKEN`. The registry workflow needs no secret
> (OIDC), but `id-token: write` permission must be allowed for Actions.
