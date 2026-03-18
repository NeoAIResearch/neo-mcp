# Neo MCP — VPS Deployment Guide

This guide takes you from a blank Ubuntu 22.04 server to a fully working
Neo MCP remote connector reachable from Claude Code.

---

## What you need before starting

- A VPS running **Ubuntu 22.04 LTS** (1 vCPU / 1 GB RAM is enough)
- A **domain or subdomain** pointing at the VPS IP — e.g. `mcp.heyneo.so` (canonical) or your own
  - Add an A record: `mcp.heyneo.so` → your VPS public IP
  - Wait ~5 minutes for DNS to propagate before running setup
- SSH access as **root**
- The Docker image already published to `ghcr.io/heyneo/neo-mcp-server`
  (pushed automatically by GitHub Actions on every merge to `main`)

---

## Architecture

```
Claude Code  ──HTTPS──▶  nginx (443)  ──HTTP──▶  neo-mcp (127.0.0.1:8000)
                           ↑
                      TLS cert (Let's Encrypt)
```

- nginx is the only public listener (ports 80 + 443)
- neo-mcp binds to `127.0.0.1:8000` only — not reachable directly from the internet
- UFW firewall blocks everything except 22/80/443
- No Neo API keys stored on the server — each user passes their own keys via request headers

---

## One-command setup

SSH into your VPS and run:

```bash
ssh root@YOUR_VPS_IP

# Download the deploy folder from the repo
curl -fsSL https://raw.githubusercontent.com/NeoResearchAI/MCPServer/main/neo-mcp/deploy/setup.sh \
  -o setup.sh

bash setup.sh mcp.heyneo.so
```

That's it. The script handles everything in order:
1. System update
2. UFW firewall (22 + 80 + 443 only)
3. Docker CE install
4. nginx install
5. nginx config (HTTP redirect + HTTPS reverse proxy)
6. Let's Encrypt TLS certificate (auto-renewed)
7. `/etc/neo-mcp/` config directory + env file
8. Docker image pull + container start
9. Health check

Total time: ~3–5 minutes.

---

## Verify it's working

```bash
# From the VPS
curl http://127.0.0.1:8000/health

# From your local machine
curl https://mcp.heyneo.so/health

# Both should return:
# {"status":"ok","server":"neo-mcp","transport":"http"}
```

---

## Add to Claude Code

### CLI
```bash
claude mcp add --transport http neo https://mcp.heyneo.so/mcp \
  --header "x-access-key: YOUR_NEO_API_KEY" \
  --header "Authorization: Bearer YOUR_NEO_SECRET_KEY"
```

### UI
1. Open Claude Code → **Settings** → **MCP Servers** → **Add Remote Server**
2. URL: `https://mcp.heyneo.so/mcp`
3. Headers:
   - `x-access-key`: your Neo access key (`ak-v1-...`)
   - `Authorization`: `Bearer sk-v1-...` (your Neo secret key)

### Verify tools are visible
```bash
/mcp
```
You should see the 7 Neo tools listed.

---

## Updating to a new version

When a new image is pushed to `ghcr.io`, redeploy with one command:

```bash
bash /etc/neo-mcp/deploy.sh
```

This pulls the latest image, recreates the container, and runs a health check.

### Auto-deploy on push (optional)

Add this to `.github/workflows/publish-mcp.yml` after the Docker build step:

```yaml
- name: Deploy to VPS
  uses: appleboy/ssh-action@v1
  with:
    host: ${{ secrets.VPS_HOST }}
    username: root
    key: ${{ secrets.VPS_SSH_KEY }}
    script: bash /etc/neo-mcp/deploy.sh
```

Add `VPS_HOST` (your domain/IP) and `VPS_SSH_KEY` (private key) to GitHub repo secrets.

---

## Useful commands on the VPS

```bash
# Live logs
docker compose -f /etc/neo-mcp/docker-compose.yml logs -f

# Restart
docker compose -f /etc/neo-mcp/docker-compose.yml restart

# Stop
docker compose -f /etc/neo-mcp/docker-compose.yml down

# Container status + health
docker ps

# nginx logs
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log

# Check TLS cert expiry
certbot certificates

# Manual cert renewal (auto-renews via cron at 3am daily)
certbot renew --dry-run
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl /health` returns connection refused | Container not running | `docker compose -f /etc/neo-mcp/docker-compose.yml up -d` |
| `curl /health` returns 502 Bad Gateway | nginx up, container down | Check `docker ps` and container logs |
| `curl /health` returns SSL error | Cert not issued yet | Re-run `certbot certonly --webroot ...` |
| Claude Code shows "connection refused" | DNS not propagated yet | Wait and retry; `dig mcp.heyneo.so` to check |
| MCP returns 401 | Missing auth headers | Add `x-access-key` and `Authorization` headers in Claude Code |
| MCP returns 400 | Neo extension not connected | Open Neo VS Code extension and connect |

---

## File layout on the VPS

```
/etc/neo-mcp/
├── .env               # server env vars (NEO_TRANSPORT, port, etc.)
├── docker-compose.yml # container definition
└── deploy.sh          # redeploy script

/etc/nginx/sites-available/neo-mcp   # nginx site config
/etc/letsencrypt/live/<domain>/       # TLS certificates
```
