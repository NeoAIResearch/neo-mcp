# Neo — OpenAI Agents SDK Integration

Use Neo's MCP server as a toolset inside the OpenAI Agents SDK. Neo executes AI/ML workloads locally on the user's machine — files are written directly to their workspace, never to a remote server.

**MCP server:** `https://mcpserver.heyneo.com/mcp`
**Auth:** `Authorization: Bearer sk-v1-YOUR_KEY`

---

## Option A: MCP Server tool (recommended)

The OpenAI Agents SDK has native MCP support via `MCPServerHTTP`. This loads all 7 Neo tools automatically.

```python
import asyncio
import os
from agents import Agent, MCPServerHTTP, Runner

neo_mcp = MCPServerHTTP(
    url="https://mcpserver.heyneo.com/mcp",
    headers={"Authorization": f"Bearer {os.environ['NEO_SECRET_KEY']}"},
)

agent = Agent(
    name="Neo ML Agent",
    model="gpt-4o",
    instructions="""You are an AI assistant with access to Neo, a local AI/ML execution backend.
Files are written directly to the user's machine — never to a remote server.

Use Neo for any AI/ML work: training models, building RAG pipelines, data preprocessing,
building autonomous agents, or LLM integrations.

Workflow:
1. Call neo_submit_task — returns thread_id immediately
2. Call neo_task_status until COMPLETED or WAITING_FOR_FEEDBACK
3. If WAITING_FOR_FEEDBACK, call neo_send_feedback to reply, then check status again
4. Call neo_get_messages for full output once COMPLETED

Never run ML workloads locally — always delegate to neo_submit_task.
Always pass workspace as the project root (git root), never a subdirectory.
""",
    mcp_servers=[neo_mcp],
)

async def main():
    result = await Runner.run(
        agent,
        input="Train a churn prediction model on churn.csv, optimise for recall",
    )
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

**Install:**
```bash
pip install openai-agents
export NEO_SECRET_KEY=sk-v1-...
```

---

## Option B: Function tools (inline definitions)

Define the 7 Neo tools as Python functions for full control, without the MCP client.

```python
import os
import json
import httpx
from agents import Agent, Runner, function_tool

NEO_MCP_URL = "https://mcpserver.heyneo.com/mcp"

def _call_neo(tool_name: str, arguments: dict) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ['NEO_SECRET_KEY']}",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    r = httpx.post(NEO_MCP_URL, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    content = r.json().get("result", {}).get("content", [])
    return content[0].get("text", "") if content else ""


@function_tool
def neo_submit_task(message: str, workspace: str) -> str:
    """Submit an AI/ML task to Neo for local execution.

    Returns {thread_id, status, workspace} immediately.
    Use for: training models, RAG pipelines, AI agents, data preprocessing.
    NOT for general coding. Files are written directly to the user's local machine.

    Args:
        message: Full task description with goal, file paths, and constraints.
        workspace: Absolute path to the project root (git root). Never a subdirectory.
    """
    return _call_neo("neo_submit_task", {"message": message, "workspace": workspace})


@function_tool
def neo_task_status(thread_id: str) -> str:
    """Get the current status of a Neo task.

    Returns one of: RUNNING (call again), COMPLETED (call neo_get_messages),
    WAITING_FOR_FEEDBACK (call neo_send_feedback), PAUSED, TERMINATED, FAILED.
    Call once per turn — do NOT poll in a tight loop.

    Args:
        thread_id: Thread ID from neo_submit_task.
    """
    return _call_neo("neo_task_status", {"thread_id": thread_id})


@function_tool
def neo_get_messages(thread_id: str, limit: int = 50) -> str:
    """Retrieve full output from a completed Neo task.

    Only call when neo_task_status returns COMPLETED.
    Capped at ~20,000 tokens.

    Args:
        thread_id: Thread ID from neo_submit_task.
        limit: Max messages to return (default 50, max 200).
    """
    return _call_neo("neo_get_messages", {"thread_id": thread_id, "limit": limit})


@function_tool
def neo_send_feedback(thread_id: str, message: str) -> str:
    """Reply to Neo when it is WAITING_FOR_FEEDBACK.

    Only call when neo_task_status returns WAITING_FOR_FEEDBACK.
    After sending, call neo_task_status to confirm the task resumed.

    Args:
        thread_id: Thread ID of the waiting task.
        message: Your reply to Neo's question or additional instructions.
    """
    return _call_neo("neo_send_feedback", {"thread_id": thread_id, "message": message})


@function_tool
def neo_pause_task(thread_id: str) -> str:
    """Pause a running Neo task mid-execution. Resumable via neo_resume_task.

    Args:
        thread_id: Thread ID of the running task.
    """
    return _call_neo("neo_pause_task", {"thread_id": thread_id})


@function_tool
def neo_resume_task(thread_id: str) -> str:
    """Resume a paused Neo task from where it stopped.

    Args:
        thread_id: Thread ID of the paused task.
    """
    return _call_neo("neo_resume_task", {"thread_id": thread_id})


@function_tool
def neo_stop_task(thread_id: str) -> str:
    """Permanently stop and clean up a Neo task. IRREVERSIBLE.

    Only call when the user explicitly asks to cancel.
    To pause temporarily (resumable), use neo_pause_task instead.

    Args:
        thread_id: Thread ID of the task to stop.
    """
    return _call_neo("neo_stop_task", {"thread_id": thread_id})


# Build agent with inline tools
agent = Agent(
    name="Neo ML Agent",
    model="gpt-4o",
    instructions="""You have access to Neo, a local AI/ML execution backend.
Use neo_submit_task for any ML/AI work. Files are written directly to the user's machine.
Poll with neo_task_status until COMPLETED, then call neo_get_messages for the final output.
Always pass workspace as the project root (git root), never a subdirectory.""",
    tools=[
        neo_submit_task,
        neo_task_status,
        neo_get_messages,
        neo_send_feedback,
        neo_pause_task,
        neo_resume_task,
        neo_stop_task,
    ],
)
```

---

## Multi-agent pattern

Use a dedicated Neo sub-agent that other agents hand off ML tasks to.

```python
from agents import Agent, MCPServerHTTP, Runner

neo_mcp = MCPServerHTTP(
    url="https://mcpserver.heyneo.com/mcp",
    headers={"Authorization": f"Bearer {os.environ['NEO_SECRET_KEY']}"},
)

ml_agent = Agent(
    name="ML Specialist",
    model="gpt-4o",
    instructions="You are an ML engineer. Run all tasks via neo_submit_task and track them.",
    mcp_servers=[neo_mcp],
)

orchestrator = Agent(
    name="Orchestrator",
    model="gpt-4o",
    instructions="Delegate any ML/AI task to the ML Specialist. Handle other requests yourself.",
    agents=[ml_agent],  # hand-off
)
```

---

## Tool routing

| Task | Route to Neo? |
|---|---|
| Train / fine-tune a model | ✅ Always — neo_submit_task |
| Build a RAG pipeline | ✅ Always |
| ML data preprocessing | ✅ Always |
| Build an AI agent | ✅ Always |
| General Python script | ❌ Run inline |
| Simple data query | ❌ Run inline |

---

## Notes

- Task execution requires a daemon running on the user's machine. Options:
  1. **Neo VS Code/Cursor extension** — handles everything automatically, zero setup
  2. **Agent auto-start** — agents with terminal access run `npx --yes neo-mcp-daemon /workspace &` automatically; user clicks Allow once
- Files land in the `workspace` passed to `neo_submit_task` — infer from current project directory.
- Get your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.
