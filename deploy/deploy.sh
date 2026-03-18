#!/usr/bin/env bash
# =============================================================================
# Neo MCP — redeploy script
# Run this any time you want to pull the latest image and restart the server.
# Usage: bash /etc/neo-mcp/deploy.sh
# =============================================================================
set -euo pipefail

cd /etc/neo-mcp

echo "Pulling latest neo-mcp image..."
docker compose pull

echo "Restarting container..."
docker compose up -d --force-recreate

echo "Waiting for health check..."
sleep 5

HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health)
if [[ "$HTTP_STATUS" == "200" ]]; then
  echo "Deploy successful — neo-mcp is healthy."
else
  echo "WARNING: Health check returned HTTP $HTTP_STATUS after deploy."
  docker compose logs --tail=50
  exit 1
fi
