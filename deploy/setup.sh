#!/usr/bin/env bash
# =============================================================================
# Neo MCP — VPS setup script
# Run once as root on a fresh Ubuntu 22.04 server.
# Usage: bash setup.sh <your-domain>   e.g.  bash setup.sh mcp.yourdomain.com
# =============================================================================
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "Usage: bash setup.sh <domain>   e.g. bash setup.sh mcp.yourdomain.com"
  exit 1
fi

echo ""
echo "======================================================"
echo "  Neo MCP VPS Setup"
echo "  Domain: $DOMAIN"
echo "======================================================"
echo ""

# ── 1. System update ──────────────────────────────────────────────────────────
echo "[1/8] Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq curl ca-certificates gnupg ufw

# ── 2. Firewall ───────────────────────────────────────────────────────────────
echo "[2/8] Configuring firewall (UFW)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP  (needed for certbot challenge)
ufw allow 443/tcp   # HTTPS
ufw --force enable
echo "   UFW status:"
ufw status verbose

# ── 3. Docker ─────────────────────────────────────────────────────────────────
echo "[3/8] Installing Docker..."
if command -v docker &>/dev/null; then
  echo "   Docker already installed: $(docker --version)"
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable docker
  systemctl start docker
  echo "   Docker installed: $(docker --version)"
fi

# ── 4. nginx ──────────────────────────────────────────────────────────────────
echo "[4/8] Installing nginx..."
apt-get install -y -qq nginx
systemctl enable nginx

# ── 5. nginx site config ──────────────────────────────────────────────────────
echo "[5/8] Writing nginx config for $DOMAIN..."
cat > /etc/nginx/sites-available/neo-mcp <<NGINX
# HTTP → redirect to HTTPS (certbot will update this block)
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    # Allow certbot ACME challenge
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

# HTTPS — proxy to neo-mcp on 127.0.0.1:8000
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    # TLS — certbot will fill these in
    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options    "nosniff" always;
    add_header X-Frame-Options           "DENY" always;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # Pass original headers through
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Forward auth headers unchanged (neo keys come from client)
        proxy_pass_request_headers on;

        # Critical for MCP SSE streams — disable all buffering
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        keepalive_timeout  600s;
    }
}
NGINX

# Disable default site, enable neo-mcp
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/neo-mcp /etc/nginx/sites-enabled/neo-mcp

nginx -t
echo "   nginx config OK"

# ── 6. Certbot / TLS ──────────────────────────────────────────────────────────
echo "[6/8] Installing certbot and issuing TLS certificate..."
apt-get install -y -qq certbot python3-certbot-nginx

# Temporarily serve HTTP only (no SSL blocks yet) so certbot challenge works
cat > /etc/nginx/sites-available/neo-mcp-temp <<NGINX_TEMP
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 200 'ok'; }
}
NGINX_TEMP

ln -sf /etc/nginx/sites-available/neo-mcp-temp /etc/nginx/sites-enabled/neo-mcp
nginx -t && systemctl reload nginx

mkdir -p /var/www/certbot
certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
  --non-interactive --agree-tos --register-unsafely-without-email

# Restore the real config
ln -sf /etc/nginx/sites-available/neo-mcp /etc/nginx/sites-enabled/neo-mcp
rm -f /etc/nginx/sites-available/neo-mcp-temp
nginx -t && systemctl reload nginx
echo "   TLS certificate issued for $DOMAIN"

# Auto-renew cron (certbot installs a timer but add a cron as backup)
(crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet && systemctl reload nginx") \
  | sort -u | crontab -

# ── 7. Create env + deploy dirs ───────────────────────────────────────────────
echo "[7/8] Creating /etc/neo-mcp config directory..."
mkdir -p /etc/neo-mcp

if [[ ! -f /etc/neo-mcp/.env ]]; then
  cat > /etc/neo-mcp/.env <<ENV
NEO_TRANSPORT=http
NEO_HTTP_HOST=0.0.0.0
NEO_HTTP_PORT=8000
NEO_API_URL=https://master.heyneo.so
# Keys are NOT stored here — each user supplies them via request headers.
# You can optionally set fallback keys if you want a single-user setup:
# NEO_API_KEY=ak-v1-...
# NEO_SECRET_KEY=sk-v1-...
ENV
  chmod 600 /etc/neo-mcp/.env
  echo "   Created /etc/neo-mcp/.env"
else
  echo "   /etc/neo-mcp/.env already exists — skipping"
fi

# Copy docker-compose.yml
cp "$(dirname "$0")/docker-compose.yml" /etc/neo-mcp/docker-compose.yml

# Copy deploy script
cp "$(dirname "$0")/deploy.sh" /etc/neo-mcp/deploy.sh
chmod +x /etc/neo-mcp/deploy.sh

# ── 8. Pull image and start container ─────────────────────────────────────────
echo "[8/8] Pulling Docker image and starting neo-mcp..."
cd /etc/neo-mcp
docker compose pull
docker compose up -d
sleep 3

# Health check
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health)
if [[ "$HTTP_STATUS" == "200" ]]; then
  echo ""
  echo "======================================================"
  echo "  Setup complete!"
  echo ""
  echo "  Health check (local):  http://127.0.0.1:8000/health"
  echo "  Health check (public): https://$DOMAIN/health"
  echo ""
  echo "  Add to Claude Code:"
  echo "  claude mcp add --transport http neo https://$DOMAIN/mcp \\"
  echo "    --header \"x-access-key: YOUR_NEO_API_KEY\" \\"
  echo "    --header \"Authorization: Bearer YOUR_NEO_SECRET_KEY\""
  echo "======================================================"
else
  echo ""
  echo "  WARNING: Health check returned HTTP $HTTP_STATUS"
  echo "  Check logs with: docker compose -f /etc/neo-mcp/docker-compose.yml logs"
fi
