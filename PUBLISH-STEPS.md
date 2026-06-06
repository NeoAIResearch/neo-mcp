# How to publish neo-mcp

Repo: `NeoAIResearch/neo-mcp` · PyPI package: `neo-mcp` · Current version: `0.5.5`

Three places: **Anthropic (Claude Desktop)**, **Smithery**, **MCP registry**.

> Assumes the package is already on PyPI. Smithery and the MCP registry both rely
> on the published `neo-mcp` package, so the matching version must be live on PyPI
> first (0.5.5 already is).

---

## Step 0 — Bump the version (only for a NEW release)

Set the same new number (e.g. `0.5.6`) in all of these files, then commit & push:

- `python/pyproject.toml` → `version = "0.5.6"`
- `.mcp/server.json` → both `"version": "0.5.6"` lines
- `mcpb/manifest.json` → `"version": "0.5.6"`
- `CLAUDE.md` → the "Current pip version" line

> Skip Step 0 if you're publishing the version that's already current.

---

## 1. Anthropic — Claude Desktop extension (`.mcpb`)

Build the bundle:

```bash
./mcpb/build.sh
npx @anthropic-ai/mcpb validate mcpb/manifest.json
```

This creates `mcpb/dist/neo-mcp-<version>.mcpb`.

**Test it locally:**
1. Open **Claude Desktop** → **Settings** → **Extensions**
2. Drag the `.mcpb` file into the window
3. Enter your **Neo Secret Key** and pick a **Workspace Directory** when prompted

**Submit to Anthropic's directory:** follow Anthropic's extension submission process
and upload the `.mcpb` file built above.

---

## 2. Smithery (web only — no upload)

1. Go to https://smithery.ai → **Sign in with GitHub**
2. Authorize Smithery's GitHub app for the **NeoAIResearch** org
3. **Add / Deploy a server** → connect GitHub → select **`NeoAIResearch/neo-mcp`**
4. Smithery reads `smithery.yaml` (runs `uvx neo-mcp`) → confirm / publish

`uvx neo-mcp` always pulls the latest PyPI version, so you do **not** re-do Smithery
for each release.

---

## 3. MCP registry (registry.modelcontextprotocol.io)

Publishes `.mcp/server.json`. Requires that you own the **NeoAIResearch** GitHub org
(the `io.github.NeoAIResearch/neo-mcp` namespace is verified through GitHub login).

**Install the publisher tool (once):**

```bash
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher
```

**Log in (interactive — opens GitHub, approve as a NeoAIResearch owner):**

```bash
./mcp-publisher login github
```

**Publish:**

```bash
./mcp-publisher publish .mcp/server.json
```

The registry downloads the PyPI package and checks its README contains
`mcp-name: io.github.NeoAIResearch/neo-mcp` (already in place).
Check it worked: https://registry.modelcontextprotocol.io

---

## Quick reference

| Place | What to do | Repeat per release? |
|---|---|---|
| Anthropic | `./mcpb/build.sh` → submit `.mcpb` | Yes (rebuild bundle) |
| Smithery | Connect repo on smithery.ai once | No (auto-tracks PyPI) |
| MCP registry | `mcp-publisher login github` → `publish .mcp/server.json` | Yes (after PyPI is updated) |
