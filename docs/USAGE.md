# Neo MCP — Usage & Deployment Guide

---

## For users — how to use Neo after installation

### Prerequisites checklist

Before sending any task:

- [ ] Neo account with API keys (from [Neo dashboard](https://app.heyneo.so))
- [ ] Neo VS Code extension installed and showing **Connected** in the sidebar
- [ ] MCP server registered in your LLM client (see `SETUP.md`)
- [ ] `NEO_API_KEY` and `NEO_SECRET_KEY` set in the MCP config

---

### How a task works end-to-end

Every Neo task follows this lifecycle. Your LLM client handles this automatically once the MCP server is registered:

```
You type a prompt
      ↓
neo_submit_task        → Neo queues the task, returns thread_id
      ↓
neo_task_status        → poll every 10–15 seconds
      ↓
   RUNNING             → keep polling
   WAITING_FOR_FEEDBACK → Neo has a question → neo_send_feedback
   COMPLETED           → neo_get_messages → read the output
   TERMINATED          → task failed or was stopped
```

> Never poll faster than every 10 seconds. Tasks take minutes — polling every second wastes quota and doesn't speed anything up.

---

### Example prompts to try

Paste these directly into Claude Code, Claude Desktop, Cursor, or any MCP-enabled client:

**Train a model**
```
Use Neo to build a churn prediction model on churn.csv, optimise for recall
```

**Data analysis**
```
Use Neo to analyse my dataset.csv and suggest the best ML approach
```

**Feature engineering**
```
Use Neo to engineer features from transactions.csv for a fraud detection model
```

**Check a running task**
```
Check the status of Neo task thr_abc123
```

**Read completed output**
```
Get the messages from Neo task thr_abc123
```

---

### Handling WAITING_FOR_FEEDBACK

When Neo needs clarification it pauses and waits. Your LLM client will automatically call `neo_send_feedback` if you reply in the chat. You can also be explicit:

```
Tell Neo: use the timestamp column as the time index
```

---

### Pausing and resuming

```
Pause Neo task thr_abc123
Resume Neo task thr_abc123
```

---

### Stopping a task

```
Stop Neo task thr_abc123
```

Add `and delete remote artifacts` if you want to clean up files on the Neo backend too.

---

### Output truncation

Neo tasks can produce large outputs. The MCP server caps responses at ~20 000 tokens (~80 000 characters). If you see:

```
[Output truncated at ~20 000 tokens. Full output available in VS Code.]
```

Open VS Code — the full output is in the Neo sidebar under the task thread.

---

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No available deployment` (400) | VS Code extension not connected | Open VS Code, check Neo sidebar shows Connected |
| `Invalid API key` (401) | Wrong keys | Re-check `NEO_API_KEY` and `NEO_SECRET_KEY` in MCP config |
| `Trial or quota ended` (403) | Account out of credits | Top up at Neo dashboard |
| LLM polls every second | Tool descriptions not loaded | Restart your LLM client |
| Task starts but nothing happens in VS Code | Extension signed into wrong account | Check the account in Neo sidebar matches your API keys |
| `neo-mcp` command not found | pip install didn't complete | Re-run `pip install git+...`, check your PATH |
| Docker sandbox ID not found | `.neo` volume not mounted | Add `-v ~/.neo:/root/.neo:ro` to the Docker run command |

---

## For maintainers — how to deploy

### What needs to be deployed

```
ghcr.io/heyneo/neo-mcp-server   ← Docker image (primary distribution)
```

Users who use pip install pull directly from GitHub — no separate publish step needed for that.

---

### One-time setup

**1. Enable GitHub Container Registry**

Go to your GitHub org → **Settings → Packages** → ensure Container registry is enabled.

**2. Make the package public** (so users don't need to authenticate to pull)

After the first push, go to `ghcr.io/heyneo/neo-mcp-server` → **Package settings → Change visibility → Public**.

**3. Grant Actions write permission**

Repo → **Settings → Actions → General → Workflow permissions** → set to **Read and write**.

---

### Automatic deploy (GitHub Actions)

The workflow at `.github/workflows/publish-mcp.yml` triggers automatically on every push to `main` that changes files in `neo-mcp/`.

```
push to main (neo-mcp/** changed)
        ↓
builds Docker image from neo-mcp/Dockerfile
        ↓
pushes ghcr.io/heyneo/neo-mcp-server:latest
pushes ghcr.io/heyneo/neo-mcp-server:{git-sha}
```

Nothing else to do — merge to main and the image is live within ~2 minutes.

---

### Manual deploy (if you need to push without a commit)

```bash
# Build
docker build -t ghcr.io/heyneo/neo-mcp-server:latest neo-mcp/

# Login
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Push
docker push ghcr.io/heyneo/neo-mcp-server:latest
```

---

### Verify the published image works

```bash
# Pull the latest
docker pull ghcr.io/heyneo/neo-mcp-server:latest

# Smoke test — should start and wait for MCP input (Ctrl+C to exit)
docker run -i --rm \
  -e NEO_API_KEY=your-key \
  -e NEO_SECRET_KEY=your-secret \
  -v ~/.neo:/root/.neo:ro \
  ghcr.io/heyneo/neo-mcp-server:latest
```

If it starts without errors (no ValueError about missing keys), the image is good.

---

### Register Claude Code against the published image

```bash
# Remove any existing local registration
claude mcp remove neo

# Add using the published Docker image
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key \
  -e NEO_SECRET_KEY=your-secret-key \
  -- docker run -i --rm \
     -e NEO_API_KEY -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro \
     ghcr.io/heyneo/neo-mcp-server

# Confirm
claude mcp list
```

Then test end-to-end:
```
Use Neo to list the files in my current workspace.
```

Expected flow:
1. Claude calls `neo_submit_task` → returns `thread_id`
2. Claude calls `neo_task_status` → `RUNNING`
3. Claude waits 10–15 seconds, polls again (not every second)
4. Status → `COMPLETED`
5. Claude calls `neo_get_messages` → shows output

---

### Versioning

The workflow tags every image with both `latest` and the git SHA:

```
ghcr.io/heyneo/neo-mcp-server:latest          ← always current
ghcr.io/heyneo/neo-mcp-server:abc1234         ← pinned to a commit
```

To pin users to a specific version, give them:
```bash
docker run -i --rm -e NEO_API_KEY -e NEO_SECRET_KEY \
  -v ~/.neo:/root/.neo:ro \
  ghcr.io/heyneo/neo-mcp-server:abc1234
```

To release a named version, tag the commit and update the workflow to also push a `v0.x.x` tag.
