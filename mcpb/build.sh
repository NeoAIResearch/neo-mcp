#!/usr/bin/env bash
# Build the Neo .mcpb bundle (uv runtime).
#
#   ./mcpb/build.sh
#
# Produces mcpb/dist/neo-mcp-<version>.mcpb — a cross-platform Claude Desktop /
# Anthropic extension. It bundles the neo_mcp source + pyproject.toml; at install
# time Claude Desktop's bundled `uv` fetches a matching Python (>=3.11) and
# installs the correct per-platform dependency wheels. No compiled wheels are
# vendored, so a single .mcpb works on macOS, Windows, and Linux.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY_SRC="$ROOT/python"
BUILD="$HERE/build"
DIST="$HERE/dist"

VERSION="$(grep -E '^version' "$PY_SRC/pyproject.toml" | head -1 | sed -E 's/.*"(.*)".*/\1/')"
echo ">> Building neo-mcp .mcpb v$VERSION (uv runtime)"

# 1. Clean
rm -rf "$BUILD" "$DIST"
mkdir -p "$BUILD/src" "$DIST"

# 2. Manifest + assets
cp "$HERE/manifest.json" "$BUILD/manifest.json"
[ -f "$HERE/icon.png" ] && cp "$HERE/icon.png" "$BUILD/icon.png"
[ -f "$HERE/.mcpbignore" ] && cp "$HERE/.mcpbignore" "$BUILD/.mcpbignore"
cp "$PY_SRC/README.md" "$BUILD/README.md" 2>/dev/null || true

# 3. Bundle the project: pyproject.toml (defines the neo-mcp package + deps) and
#    the neo_mcp source tree. `uv run` builds/installs it on first launch.
cp "$PY_SRC/pyproject.toml" "$BUILD/pyproject.toml"
cp -R "$PY_SRC/src/neo_mcp" "$BUILD/src/neo_mcp"

# 4. Trim bytecode
find "$BUILD/src" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/src" -name "*.pyc" -delete 2>/dev/null || true

# 5. Pack
OUT="$DIST/neo-mcp-$VERSION.mcpb"
echo ">> Packing $OUT"
npx --yes @anthropic-ai/mcpb pack "$BUILD" "$OUT"

echo ">> Done: $OUT"
ls -lh "$OUT"
