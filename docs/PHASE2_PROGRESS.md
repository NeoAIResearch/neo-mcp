# Phase 2 — VPS Deployment: Progress Log

Goal: run neo-mcp HTTP server on a VPS with a public HTTPS URL so it can
be added as a remote connector in Claude Code.

## Steps

- [ ] **1. Provision a VPS**
  - Recommended: DigitalOcean Droplet, Hetzner Cloud, or Linode
  - Minimum spec: 1 vCPU, 1 GB RAM, 10 GB disk (the server is very lightweight)
  - OS: Ubuntu 22.04 LTS
  - Enable SSH key auth, disable password login

- [ ] **2. Point a domain/subdomain at the VPS**
  - Add an A record: `mcp.yourdomain.com` → VPS public IP
  - TTL: 300 (propagates in ~5 min)
  - A domain is required for HTTPS (certbot needs it)

- [ ] **3. SSH in and install Docker**
  ```bash
  ssh root@YOUR_VPS_IP
  apt update && apt upgrade -y
  apt install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt update && apt install -y docker-ce docker-ce-cli containerd.io
  docker --version   # verify
  ```

- [ ] **4. Install nginx + certbot**
  ```bash
  apt install -y nginx certbot python3-certbot-nginx
  ```

- [ ] **5. Configure nginx as reverse proxy**
  Create `/etc/nginx/sites-available/neo-mcp`:
  ```nginx
  server {
      listen 80;
      server_name mcp.yourdomain.com;

      location / {
          proxy_pass         http://127.0.0.1:8000;
          proxy_http_version 1.1;
          proxy_set_header   Upgrade $http_upgrade;
          proxy_set_header   Connection keep-alive;
          proxy_set_header   Host $host;
          proxy_set_header   X-Real-IP $remote_addr;
          proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header   X-Forwarded-Proto $scheme;
          # Required for MCP SSE streams
          proxy_buffering    off;
          proxy_cache        off;
          proxy_read_timeout 600s;
      }
  }
  ```
  ```bash
  ln -s /etc/nginx/sites-available/neo-mcp /etc/nginx/sites-enabled/
  nginx -t && systemctl reload nginx
  ```

- [ ] **6. Issue TLS certificate**
  ```bash
  certbot --nginx -d mcp.yourdomain.com
  # Follow prompts — certbot auto-edits nginx config for HTTPS
  systemctl reload nginx
  ```

- [ ] **7. Create env file for Docker**
  ```bash
  mkdir -p /etc/neo-mcp
  cat > /etc/neo-mcp/.env <<EOF
  NEO_TRANSPORT=http
  NEO_HTTP_HOST=0.0.0.0
  NEO_HTTP_PORT=8000
  NEO_API_URL=https://master.heyneo.so
  EOF
  chmod 600 /etc/neo-mcp/.env
  ```
  Note: NEO_API_KEY / NEO_SECRET_KEY are NOT set here —
  each user supplies their own keys via request headers.

- [ ] **8. Pull and run the Docker container**
  ```bash
  docker pull ghcr.io/heyneo/neo-mcp-server:latest
  docker run -d \
    --name neo-mcp \
    --restart unless-stopped \
    --env-file /etc/neo-mcp/.env \
    -p 127.0.0.1:8000:8000 \
    ghcr.io/heyneo/neo-mcp-server:latest
  docker logs neo-mcp   # should show "listening on 0.0.0.0:8000"
  ```
  Port is bound to 127.0.0.1 only — nginx is the only public entry point.

- [ ] **9. Verify end-to-end**
  ```bash
  curl https://mcp.yourdomain.com/health
  # expect: {"status":"ok","server":"neo-mcp","transport":"http"}
  ```

- [ ] **10. Add to Claude Code**
  ```bash
  claude mcp add --transport http neo https://mcp.yourdomain.com/mcp \
    --header "x-access-key: YOUR_NEO_API_KEY" \
    --header "Authorization: Bearer YOUR_NEO_SECRET_KEY"
  ```
  Or from Claude Code UI:
  Settings → MCP Servers → Add Remote Server → enter URL + headers

- [ ] **11. Set up auto-deploy on push (optional)**
  On the VPS, create `/etc/neo-mcp/deploy.sh`:
  ```bash
  #!/bin/bash
  docker pull ghcr.io/heyneo/neo-mcp-server:latest
  docker stop neo-mcp && docker rm neo-mcp
  docker run -d \
    --name neo-mcp \
    --restart unless-stopped \
    --env-file /etc/neo-mcp/.env \
    -p 127.0.0.1:8000:8000 \
    ghcr.io/heyneo/neo-mcp-server:latest
  ```
  ```bash
  chmod +x /etc/neo-mcp/deploy.sh
  ```
  Then add a GitHub Actions job in `publish-mcp.yml` that SSHes in and
  runs `deploy.sh` after the Docker image is pushed to ghcr.io.

## Useful commands

```bash
# View live logs
docker logs -f neo-mcp

# Restart container
docker restart neo-mcp

# Update to latest image
/etc/neo-mcp/deploy.sh

# Check nginx logs
tail -f /var/log/nginx/error.log
tail -f /var/log/nginx/access.log

# Renew TLS cert (auto-renewed by certbot timer, but manual if needed)
certbot renew --dry-run
```
