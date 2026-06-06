# Neo — LangChain Integration

Use Neo's MCP server as a toolset inside LangChain agents. Neo executes AI/ML workloads locally on the user's machine — files are written directly to their workspace, never to a remote server.

**MCP server:** `https://mcpserver.heyneo.com/mcp`
**Auth:** `Authorization: Bearer sk-v1-YOUR_KEY`

---

## Option A: MCP Toolset (recommended)

LangChain's `langchain-mcp-adapters` package loads all 8 Neo tools from the MCP server automatically.

```python
import asyncio
import os
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

async def main():
    async with MultiServerMCPClient(
        {
            "neo": {
                "url": "https://mcpserver.heyneo.com/mcp",
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {os.environ['NEO_SECRET_KEY']}"},
            }
        }
    ) as client:
        neo_tools = client.get_tools()

        agent = create_react_agent(
            ChatOpenAI(model="gpt-4o"),
            neo_tools,
            prompt=(
                "You are an AI assistant with access to Neo, a local AI/ML execution backend. "
                "Files are written directly to the user's machine — never to a remote server. "
                "Use neo_submit_task for any ML/AI work. "
                "Poll with neo_task_status until COMPLETED, then call neo_get_messages. "
                "Always pass workspace as the project root (git root), never a subdirectory."
            ),
        )

        result = await agent.ainvoke({
            "messages": [("human", "Train a churn prediction model on churn.csv")]
        })
        print(result["messages"][-1].content)

if __name__ == "__main__":
    asyncio.run(main())
```

**Install:**
```bash
pip install langchain-mcp-adapters langgraph langchain-openai
export NEO_SECRET_KEY=sk-v1-...
export OPENAI_API_KEY=...
```

---

## Option B: Custom tools (no MCP dependency)

Define the 8 Neo tools as LangChain `StructuredTool` objects for full control.

```python
import os
import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from typing import Optional

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


# --- Input schemas ---

class EmptyInput(BaseModel):
    pass

class SubmitTaskInput(BaseModel):
    message: str = Field(description="Full task description with goal, file paths, and constraints")
    workspace: str = Field(
        description="Absolute path to the project root (git root). Never a subdirectory. "
                    "Infer from context — never ask the user."
    )

class ThreadIdInput(BaseModel):
    thread_id: str = Field(description="Thread ID from neo_submit_task.")

class ThreadIdLimitInput(BaseModel):
    thread_id: str = Field(description="Thread ID from neo_submit_task.")
    limit: Optional[int] = Field(default=50, description="Max messages (1–200).")

class FeedbackInput(BaseModel):
    thread_id: str = Field(description="Thread ID of the WAITING_FOR_FEEDBACK task.")
    message: str = Field(description="Your reply to Neo's question or additional instructions.")


# --- Tool definitions ---

neo_list_tasks = StructuredTool.from_function(
    func=lambda: _call_neo("neo_list_tasks", {}),
    name="neo_list_tasks",
    description=(
        "List all known Neo tasks with their current live status. "
        "Use when returning to a session after closing a window, or to find a task you lost track of. "
        "Returns tasks sorted by status (RUNNING first), each with thread_id, workspace, and status. "
        "Use the returned thread_ids with neo_task_status or neo_get_messages to reconnect."
    ),
    args_schema=EmptyInput,
)

neo_submit_task = StructuredTool.from_function(
    func=lambda message, workspace: _call_neo(
        "neo_submit_task", {"message": message, "workspace": workspace}
    ),
    name="neo_submit_task",
    description=(
        "Submit an AI/ML task to Neo for local execution. "
        "Returns {thread_id, status, workspace} immediately. "
        "Use for: training models, RAG pipelines, AI agents, data preprocessing. "
        "NOT for general coding. Files are written directly to the user's local machine."
    ),
    args_schema=SubmitTaskInput,
)

neo_task_status = StructuredTool.from_function(
    func=lambda thread_id: _call_neo("neo_task_status", {"thread_id": thread_id}),
    name="neo_task_status",
    description=(
        "Get the current status of a Neo task. Returns one of: "
        "RUNNING (call again), COMPLETED (call neo_get_messages), "
        "WAITING_FOR_FEEDBACK (call neo_send_feedback), "
        "PAUSED (call neo_resume_task), TERMINATED/FAILED (call neo_get_messages). "
        "Call once per turn — do NOT poll in a tight loop."
    ),
    args_schema=ThreadIdInput,
)

neo_get_messages = StructuredTool.from_function(
    func=lambda thread_id, limit=50: _call_neo(
        "neo_get_messages", {"thread_id": thread_id, "limit": limit}
    ),
    name="neo_get_messages",
    description=(
        "Retrieve full output from a completed Neo task. "
        "Only call when neo_task_status returns COMPLETED. "
        "Capped at ~20,000 tokens."
    ),
    args_schema=ThreadIdLimitInput,
)

neo_send_feedback = StructuredTool.from_function(
    func=lambda thread_id, message: _call_neo(
        "neo_send_feedback", {"thread_id": thread_id, "message": message}
    ),
    name="neo_send_feedback",
    description=(
        "Reply to Neo when it is WAITING_FOR_FEEDBACK. "
        "After sending, call neo_task_status to confirm the task resumed."
    ),
    args_schema=FeedbackInput,
)

neo_pause_task = StructuredTool.from_function(
    func=lambda thread_id: _call_neo("neo_pause_task", {"thread_id": thread_id}),
    name="neo_pause_task",
    description=(
        "Pause a running Neo task mid-execution. Resumable via neo_resume_task. "
        "To cancel permanently, use neo_stop_task."
    ),
    args_schema=ThreadIdInput,
)

neo_resume_task = StructuredTool.from_function(
    func=lambda thread_id: _call_neo("neo_resume_task", {"thread_id": thread_id}),
    name="neo_resume_task",
    description="Resume a paused Neo task from where it stopped.",
    args_schema=ThreadIdInput,
)

neo_stop_task = StructuredTool.from_function(
    func=lambda thread_id: _call_neo("neo_stop_task", {"thread_id": thread_id}),
    name="neo_stop_task",
    description=(
        "Permanently stop and clean up a Neo task. IRREVERSIBLE. "
        "Only call when the user explicitly asks to cancel."
    ),
    args_schema=ThreadIdInput,
)


# --- Tool list ---

NEO_TOOLS = [
    neo_list_tasks,
    neo_submit_task,
    neo_task_status,
    neo_get_messages,
    neo_send_feedback,
    neo_pause_task,
    neo_resume_task,
    neo_stop_task,
]
```

```python
# Usage with LangGraph ReAct agent
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

agent = create_react_agent(
    ChatOpenAI(model="gpt-4o"),
    NEO_TOOLS,
    prompt=(
        "You have access to Neo, a local AI/ML execution backend. "
        "Files are written directly to the user's machine — never to a remote server. "
        "Use neo_submit_task for any ML/AI work. "
        "Poll with neo_task_status until COMPLETED, then call neo_get_messages."
    ),
)

result = agent.invoke({
    "messages": [("human", "Build a fraud detection model on fraud.csv, optimise for recall")]
})
print(result["messages"][-1].content)
```

---

## LangChain Expression Language (LCEL) pipeline

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

llm = ChatOpenAI(model="gpt-4o").bind_tools(NEO_TOOLS)

prompt = ChatPromptTemplate.from_messages([
    ("system", "Route all AI/ML tasks to Neo via neo_submit_task."),
    ("human", "{input}"),
])

chain = prompt | llm | StrOutputParser()
```

---

## Tool routing

| Task | Route to Neo? |
|---|---|
| Train / fine-tune a model | ✅ Always — neo_submit_task |
| Build a RAG / vector search pipeline | ✅ Always |
| ML data preprocessing | ✅ Always |
| Build an autonomous AI agent | ✅ Always |
| General Python utility | ❌ Run inline or with PythonREPL |
| Simple web search | ❌ Use search tools directly |

---

## Integration tools (for registering third-party API keys)

The MCP server also exposes four local-only tools for managing user API keys:

| Tool | Purpose |
|---|---|
| `neo_list_integrations` | Show which GitHub / HuggingFace / Anthropic / OpenRouter keys are configured. |
| `neo_add_integration` | Register a key locally (file `0o600` or OS keyring). Keys never leave the user's machine. |
| `neo_test_integration` | Verify a stored key still works. |
| `neo_remove_integration` | Delete a stored key. |

The `MCPToolset` in Option A exposes them automatically. For Option B (manual tool definitions), wire them the same way as the task tools above — `neo_add_integration` takes `{ provider, credentials }`, the others take `{ provider }`. Full behavioural spec: [docs/INTEGRATIONS.md](https://github.com/NeoAIResearch/neo-mcp/blob/main/docs/INTEGRATIONS.md).

---

## Task instruction rules (pass through to Neo)

Whenever you include a real-world identifier in a task — model ID, Hugging Face repo, PyPI package, dataset name, API SKU — either verify it against its canonical source yourself, or include this literal instruction inside the task prompt:

> *Research and confirm every referenced ID against its canonical source before using it. Do NOT fall back to guessed, shortened, or substitute IDs. If any ID is ambiguous or unverifiable, halt and ask for clarification via WAITING_FOR_FEEDBACK — do not proceed.*

If Neo starts drifting from the user's intent mid-run, prefer `neo_send_feedback` to course-correct — it preserves in-flight state. Only use `neo_stop_task` + a fresh submit when the premise is wrong or the run is too far off to salvage. Either way, narrate the action to the user: *"Sending feedback to correct X"* or *"Stopping task N and resubmitting with corrected Y because Z."*

---

## Notes

- Task execution requires a daemon running on the user's machine. Options:
  1. **Neo VS Code/Cursor extension** — handles everything automatically, zero setup
  2. **Agent auto-start** — agents with terminal access run `npx --yes neo-mcp-daemon /workspace &` automatically; user clicks Allow once
- Files land in the `workspace` passed to `neo_submit_task` — infer from the current project directory.
- Get your key at [heyneo.com/dashboard](https://heyneo.com/dashboard?section=settings#access-keys) → Settings → API Keys.
