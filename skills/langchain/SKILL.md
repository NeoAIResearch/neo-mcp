# Neo — LangChain Integration

Use Neo's MCP server as a toolset inside LangChain agents. Neo runs AI/ML workloads remotely so your agent doesn't need to execute training or data-processing jobs inline.

**MCP server:** `https://mcpserver.heyneo.com/mcp`
**Auth:** `Authorization: Bearer sk-v1-YOUR_KEY`

---

## Option A: MCP Toolset (recommended)

LangChain's `langchain-mcp-adapters` package loads all 9 Neo tools from the MCP server automatically.

```pythontest
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
                "You are an AI assistant with access to Neo, a remote AI/ML backend. "
                "Use neo_submit_task for any ML/AI work. "
                "Poll with neo_task_plan until COMPLETED, then use neo_get_messages."
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

Define Neo tools as LangChain `StructuredTool` objects for full control.

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

class SubmitTaskInput(BaseModel):
    description: str = Field(description="Full description of the AI/ML task to run")
    wait_for_completion: Optional[bool] = Field(
        default=False,
        description="Block until done and return output directly (tasks under ~3 min only)"
    )
    auto_mode: Optional[bool] = Field(
        default=False,
        description="Run fully autonomously without pausing for questions"
    )

class ThreadIdInput(BaseModel):
    thread_id: Optional[str] = Field(
        default=None,
        description="Thread ID from neo_submit_task. Omit to use last active thread."
    )

class FeedbackInput(BaseModel):
    message: str = Field(description="Your reply to Neo's question")
    thread_id: Optional[str] = Field(
        default=None,
        description="Thread ID. Omit for last active thread."
    )

class StopInput(BaseModel):
    thread_id: Optional[str] = Field(default=None, description="Thread ID. Omit for last active.")
    delete_remote_artifacts: Optional[bool] = Field(
        default=False,
        description="Also delete files stored on Neo's servers"
    )


# --- Tool definitions ---

neo_submit_task = StructuredTool.from_function(
    func=lambda description, wait_for_completion=False, auto_mode=False: _call_neo(
        "neo_submit_task",
        {"description": description, "wait_for_completion": wait_for_completion, "auto_mode": auto_mode},
    ),
    name="neo_submit_task",
    description=(
        "Submit an AI/ML task to Neo's remote backend. "
        "Returns thread_id immediately. Use wait_for_completion=True only for short tasks (< 3 min). "
        "Always use this for: training models, RAG pipelines, AI agents, data preprocessing."
    ),
    args_schema=SubmitTaskInput,
)

neo_task_plan = StructuredTool.from_function(
    func=lambda thread_id=None: _call_neo("neo_task_plan", {"thread_id": thread_id} if thread_id else {}),
    name="neo_task_plan",
    description=(
        "Show Neo's current execution plan with per-step status (PENDING/RUNNING/COMPLETED/FAILED). "
        "Much cheaper than neo_get_messages — use while the task is RUNNING."
    ),
    args_schema=ThreadIdInput,
)

neo_task_status = StructuredTool.from_function(
    func=lambda thread_id=None: _call_neo("neo_task_status", {"thread_id": thread_id} if thread_id else {}),
    name="neo_task_status",
    description=(
        "Check the current status of a Neo task: "
        "RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / PAUSED / TERMINATED."
    ),
    args_schema=ThreadIdInput,
)

neo_get_messages = StructuredTool.from_function(
    func=lambda thread_id=None: _call_neo("neo_get_messages", {"thread_id": thread_id} if thread_id else {}),
    name="neo_get_messages",
    description="Get the full conversation output once a task is COMPLETED. Capped at ~20 000 tokens.",
    args_schema=ThreadIdInput,
)

neo_get_files = StructuredTool.from_function(
    func=lambda thread_id=None: _call_neo("neo_get_files", {"thread_id": thread_id} if thread_id else {}),
    name="neo_get_files",
    description="Download files generated by a completed task (code, models, scripts). Returns contents inline.",
    args_schema=ThreadIdInput,
)

neo_send_feedback = StructuredTool.from_function(
    func=lambda message, thread_id=None: _call_neo(
        "neo_send_feedback",
        {"message": message, **({"thread_id": thread_id} if thread_id else {})},
    ),
    name="neo_send_feedback",
    description="Reply to Neo when it is WAITING_FOR_FEEDBACK.",
    args_schema=FeedbackInput,
)

neo_stop_task = StructuredTool.from_function(
    func=lambda thread_id=None, delete_remote_artifacts=False: _call_neo(
        "neo_stop_task",
        {
            "delete_remote_artifacts": delete_remote_artifacts,
            **({"thread_id": thread_id} if thread_id else {}),
        },
    ),
    name="neo_stop_task",
    description="Stop and clean up a running or paused task.",
    args_schema=StopInput,
)


# --- Agent setup ---

NEO_TOOLS = [
    neo_submit_task,
    neo_task_plan,
    neo_task_status,
    neo_get_messages,
    neo_get_files,
    neo_send_feedback,
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
        "You have access to Neo, a remote AI/ML execution backend. "
        "Use neo_submit_task for any ML/AI work. "
        "Poll with neo_task_plan until COMPLETED, then call neo_get_messages."
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

## Notes

- `thread_id` is optional — the server auto-recovers the last active thread.
- Task execution requires a daemon on the user's machine: install the **Neo VS Code/Cursor extension** (zero setup), or let an agent with terminal access start it automatically on first task (`neo-mcp daemon &` — uses `NEO_SECRET_KEY` directly).
- Get your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.
