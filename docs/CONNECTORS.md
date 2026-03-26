# Neo MCP — Web Connector Setup (Claude.ai + ChatGPT)

Neo MCP can be connected directly from the **Claude.ai** and **ChatGPT** web interfaces — no
installation, no config files. Just paste one URL and complete a quick OAuth flow to enter your
Neo secret key.

---

## Claude.ai

1. Open **claude.ai** → Settings → **Integrations**
2. Click **Add custom connector**
3. Enter the connector URL:
   ```
   https://mcpserver.heyneo.com/mcp
   ```
4. Click **Connect** — Claude.ai will redirect you to the Neo authorization page
5. Enter your Neo secret key (`sk-v1-...`) from the [Neo dashboard](https://app.heyneo.so)
6. Click **Authorize** — you'll be redirected back to Claude.ai automatically
7. The Neo tools (`neo_submit_task`, `neo_task_status`, etc.) now appear in every conversation

---

## ChatGPT

1. Open **chatgpt.com** → Settings → **Connectors**
2. Click **Add connector** → **Custom**
3. Enter the connector URL:
   ```
   https://mcpserver.heyneo.com/mcp
   ```
4. Click **Connect** — ChatGPT will redirect you to the Neo authorization page
5. Enter your Neo secret key (`sk-v1-...`) and click **Authorize**
6. Neo tools are now available in ChatGPT

---

## How the OAuth flow works

```
Claude.ai (or ChatGPT)
  │
  ├── GET /.well-known/oauth-protected-resource
  │         ← { authorization_servers: ["https://mcpserver.heyneo.com"] }
  │
  ├── GET /.well-known/oauth-authorization-server
  │         ← { authorization_endpoint, token_endpoint, ... }
  │
  ├── Redirect → /oauth/authorize?client_id=...&code_challenge=...&state=...
  │         User sees: "Enter your Neo secret key"
  │         User types: sk-v1-...
  │         ← Redirect back to claude.ai/chatgpt.com with ?code=<random_code>
  │
  ├── POST /oauth/token { code, code_verifier, ... }
  │         ← { access_token: "sk-v1-...", token_type: "bearer" }
  │
  └── POST /mcp  (Authorization: Bearer sk-v1-...)
            ← MCP tools: neo_submit_task, neo_task_status, etc.
```

The "access token" issued is your `NEO_SECRET_KEY` — no separate token infrastructure.
Neo's backend already validates Bearer tokens; OAuth is purely the auth-discovery layer.

---

## Security

- **PKCE (S256)** is enforced — auth codes cannot be intercepted without `code_verifier`
- Auth codes expire after **10 minutes**
- Your secret key is never logged or stored permanently (only in-memory until exchanged)
- `redirect_uri` must start with `https://` or `http://localhost` — no open redirect

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Connect" button shows an error | Verify the URL is exactly `https://mcpserver.heyneo.com/mcp` |
| Authorization page says "Invalid redirect_uri" | This is a platform issue — try again or report to Neo support |
| Tools don't appear after connecting | Refresh the page; check Settings → Integrations for an error |
| "Invalid API key" when using a tool | Re-enter your key: disconnect and reconnect the integration |
| Auth code expired | Re-authorize — the flow restarts automatically if the token expires |

---

## Getting your Neo secret key

1. Sign in at [app.heyneo.so](https://app.heyneo.so)
2. Go to **Settings → API Keys**
3. Copy your secret key (starts with `sk-v1-`)

Do not share your secret key. It grants full access to your Neo account.
