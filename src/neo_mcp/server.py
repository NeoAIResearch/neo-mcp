import asyncio
import os
import re

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server  # used as async context manager
from mcp.types import Tool, TextContent
from mcp import types

NEO_API_URL = os.environ.get("NEO_API_URL", "https://master.heyneo.so")
NEO_API_KEY = os.environ.get("NEO_API_KEY", "")      # access key (ak-v1-...)
NEO_SECRET_KEY = os.environ.get("NEO_SECRET_KEY", "") # secret key (sk-v1-...)
NEO_READ_ONLY = os.environ.get("NEO_READ_ONLY", "").lower() == "true"
NEO_DEPLOYMENT_ID = os.environ.get("NEO_DEPLOYMENT_ID", "")  # optional, override auto-discovered sandbox ID


def _discover_sandbox_id() -> str:
    """Try multiple sources to find the active Neo sandbox/deployment ID.

    Priority:
    1. daemon.log  — most recent sandboxId (works when VS Code / Cursor extension is running)
    2. thread-workspaces.json — match current working directory to a sandbox ID
    Returns empty string if nothing found.
    """
    cwd = os.getcwd()

    # 1. daemon log — works for VS Code and Cursor Neo extensions
    for log_name in ("daemon.log", "daemon.log.1"):
        log_path = os.path.expanduser(f"~/.neo/daemon/{log_name}")
        try:
            with open(log_path, "r", errors="ignore") as f:
                content = f.read()
            matches = re.findall(r'"sandboxId"\s*:\s*"([a-f0-9\-]{36})"', content)
            if matches:
                return matches[-1]
        except OSError:
            pass

    # 2. thread-workspaces.json — maps sandbox IDs to workspace paths
    ws_path = os.path.expanduser("~/.neo/daemon/thread-workspaces.json")
    try:
        import json
        with open(ws_path, "r", errors="ignore") as f:
            workspaces: dict = json.load(f)
        # Prefer exact CWD match, then parent match
        for sandbox_id, ws_dir in reversed(list(workspaces.items())):
            if cwd == ws_dir or cwd.startswith(ws_dir.rstrip("/") + "/"):
                return sandbox_id
        # Fallback: return the most recent entry
        if workspaces:
            return list(workspaces.keys())[-1]
    except (OSError, ValueError):
        pass

    return ""


def _get_deployment_id() -> str:
    """Return deployment ID — re-discovers each call so extensions that start
    after the MCP server are picked up automatically."""
    return NEO_DEPLOYMENT_ID or _discover_sandbox_id()


# Capture working directory at server startup — this is where the user launched the MCP client from
_server_cwd = os.getcwd()

if not NEO_API_KEY:
    raise ValueError("NEO_API_KEY environment variable is required but not set.")
if not NEO_SECRET_KEY:
    raise ValueError("NEO_SECRET_KEY environment variable is required but not set.")

app = Server("neo-mcp")

def handle_error(status_code: int) -> str:
    messages = {
        400: "No available deployment. Ensure the Neo VS Code extension is open and connected.",
        401: "Invalid API key. Check your NEO_API_KEY configuration.",
        402: "Your Neo account has insufficient credits.",
        403: "Your Neo trial or quota has ended.",
        404: "Thread or user not found.",
        500: "Neo backend error. Please try again.",
    }
    return messages.get(status_code, f"Unexpected error (HTTP {status_code}).")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NEO_SECRET_KEY}",
        "x-access-key": NEO_API_KEY,
    }



@app.list_tools()
async def list_tools() -> list[Tool]:
    read_tools = [
        Tool(
            name="neo_task_status",
            description=(
                "Check Neo task status. Wait 10–15 seconds between calls when status is RUNNING. "
                "Act immediately on WAITING_FOR_FEEDBACK or COMPLETED."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID returned by neo_submit_task."},
                },
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="neo_get_messages",
            description="Read the full output of a completed Neo task. Call after neo_task_status returns COMPLETED.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID to retrieve messages for."},
                },
                "required": ["thread_id"],
            },
        ),
    ]

    if NEO_READ_ONLY:
        return read_tools

    write_tools = [
        Tool(
            name="neo_submit_task",
            description=(
                "Submit a Neo task and wait for it to complete. "
                "Handles polling automatically — returns the full result when done. "
                "If Neo needs input mid-task, returns WAITING_FOR_FEEDBACK with the thread_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "The task description to send to Neo."},
                    "auto_mode": {
                        "type": "boolean",
                        "description": "Whether to run in auto mode (default: false).",
                        "default": False,
                    },
                },
                "required": ["description"],
            },
        ),
        Tool(
            name="neo_send_feedback",
            description=(
                "Send a reply to Neo when it is waiting for your input. "
                "Only call this when neo_task_status returns WAITING_FOR_FEEDBACK."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID."},
                    "message": {"type": "string", "description": "Your reply to Neo."},
                },
                "required": ["thread_id", "message"],
            },
        ),
        Tool(
            name="neo_pause_task",
            description="Pause a running Neo task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID to pause."},
                },
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="neo_resume_task",
            description="Resume a paused Neo task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID to resume."},
                },
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="neo_stop_task",
            description="Stop and clean up a Neo task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID to stop."},
                    "delete_remote_artifacts": {
                        "type": "boolean",
                        "description": "Whether to delete remote artifacts (default: false).",
                        "default": False,
                    },
                },
                "required": ["thread_id"],
            },
        ),
    ]

    return write_tools + read_tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=30.0) as client:

        if name == "neo_submit_task":
            deployment_id = _get_deployment_id()
            description = arguments["description"]
            auto_mode = arguments.get("auto_mode", False)
            message = f"Working directory: {_server_cwd}\n\nCreate all files inside this directory.\n\n{description}"

            # Submit
            resp = await client.post(
                "/v2/thread/init-chat-direct",
                headers=_headers(),
                json={
                    "message": message,
                    "deployment_type": "vscode",
                    "auto_mode": auto_mode,
                    **({"deployment_id": deployment_id} if deployment_id else {}),
                },
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            thread_id = resp.json().get("thread_id", resp.json().get("id", "unknown"))

            # Poll until terminal state (max 20 minutes)
            poll_client = httpx.AsyncClient(base_url=NEO_API_URL, timeout=30.0)
            status = "RUNNING"
            try:
                for _ in range(80):
                    await asyncio.sleep(15)
                    sr = await poll_client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
                    if sr.status_code != 200:
                        break
                    status = sr.json().get("status", "UNKNOWN")
                    if status in ("COMPLETED", "WAITING_FOR_FEEDBACK", "TERMINATED"):
                        break
            finally:
                await poll_client.aclose()

            if status == "COMPLETED":
                # Fetch and return the messages directly
                all_messages: list[dict] = []
                total_chars = 0
                char_cap = 80000
                before = None
                capped = False
                while True:
                    params: dict = {"thread_id": thread_id, "limit": 100}
                    if before is not None:
                        params["before"] = before
                    mr = await client.get("/v2/thread/thread-messages", headers=_headers(), params=params)
                    if mr.status_code != 200:
                        break
                    mdata = mr.json()
                    msgs = mdata.get("messages", [])
                    for msg in msgs:
                        content = msg.get("content", "")
                        if total_chars + len(content) > char_cap:
                            capped = True
                            break
                        all_messages.append(msg)
                        total_chars += len(content)
                    if capped or not mdata.get("has_more") or not msgs:
                        break
                    before = msgs[-1].get("created_at") or msgs[-1].get("timestamp")
                    if before is None:
                        break
                formatted = [f"[{m.get('role','?').upper()}]\n{m.get('content','')}" for m in all_messages]
                output = "\n---\n".join(formatted)
                if capped:
                    output += "\n---\n[Output truncated. Full output available in VS Code.]"
                return [TextContent(type="text", text=f"Task completed. thread_id: {thread_id}\n\n{output or 'No messages.'}")]

            if status == "WAITING_FOR_FEEDBACK":
                return [TextContent(type="text", text=(
                    f"Neo is waiting for your input. thread_id: {thread_id}\n"
                    "Use neo_send_feedback to reply, then neo_task_status to resume polling."
                ))]

            if status == "TERMINATED":
                return [TextContent(type="text", text=f"Task terminated. thread_id: {thread_id}")]

            # Still running after timeout
            return [TextContent(type="text", text=(
                f"Task is still running after 20 minutes. thread_id: {thread_id}\n"
                "Use neo_task_status to keep checking."
            ))]

        elif name == "neo_task_status":
            thread_id = arguments["thread_id"]
            resp = await client.get(
                f"/v2/thread/status/{thread_id}",
                headers=_headers(),
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            data = resp.json()
            status = data.get("status", "UNKNOWN")

            if status == "COMPLETED":
                msg = "Status: COMPLETED. Call neo_get_messages to read the output."
                return [TextContent(type="text", text=msg)]

            hints = {
                "RUNNING": "Status: RUNNING. Wait 10–15 seconds then poll again.",
                "WAITING_FOR_FEEDBACK": "Status: WAITING_FOR_FEEDBACK. Neo has a question. Call neo_send_feedback now.",
                "PAUSED": "Status: PAUSED. Call neo_resume_task to continue.",
                "TERMINATED": "Status: TERMINATED. Task was stopped or hit a fatal error.",
            }
            return [TextContent(type="text", text=hints.get(status, f"Status: {status}."))]

        elif name == "neo_get_messages":
            thread_id = arguments["thread_id"]
            all_messages: list[dict] = []
            total_chars = 0
            char_cap = 80000
            before = None
            capped = False

            while True:
                params: dict = {"thread_id": thread_id, "limit": 100}
                if before is not None:
                    params["before"] = before
                resp = await client.get("/v2/thread/thread-messages", headers=_headers(), params=params)
                if resp.status_code != 200:
                    return [TextContent(type="text", text=handle_error(resp.status_code))]
                data = resp.json()
                messages = data.get("messages", [])
                has_more = data.get("has_more", False)

                for msg in messages:
                    content = msg.get("content", "")
                    if total_chars + len(content) > char_cap:
                        capped = True
                        break
                    all_messages.append(msg)
                    total_chars += len(content)

                if capped or not has_more or not messages:
                    break

                # Use the earliest message timestamp for the next page
                before = messages[-1].get("created_at") or messages[-1].get("timestamp")
                if before is None:
                    break

            formatted = []
            for msg in all_messages:
                role = msg.get("role", "unknown").upper()
                content = msg.get("content", "")
                formatted.append(f"[{role}]\n{content}")

            output = "\n---\n".join(formatted)
            if capped:
                output += "\n---\n[Output truncated at ~20 000 tokens. Full output available in VS Code.]"
            return [TextContent(type="text", text=output or "No messages found.")]

        elif name == "neo_send_feedback":
            thread_id = arguments["thread_id"]
            message = arguments["message"]
            resp = await client.post(
                f"/v2/thread/feedback/{thread_id}",
                headers=_headers(),
                json={"input": message},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            return [TextContent(type="text", text="Feedback sent. Neo is continuing the task.")]

        elif name == "neo_pause_task":
            thread_id = arguments["thread_id"]
            resp = await client.post(
                f"/v2/thread/control/{thread_id}",
                headers=_headers(),
                json={"signal": "PAUSE"},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            return [TextContent(type="text", text=f"Task {thread_id} paused.")]

        elif name == "neo_resume_task":
            thread_id = arguments["thread_id"]
            resp = await client.post(
                f"/v2/thread/control/{thread_id}",
                headers=_headers(),
                json={"signal": "RESUME"},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            return [TextContent(type="text", text=f"Task {thread_id} resumed.")]

        elif name == "neo_stop_task":
            thread_id = arguments["thread_id"]
            delete_remote_artifacts = arguments.get("delete_remote_artifacts", False)
            resp = await client.delete(
                f"/v2/thread/cleanup-direct/{thread_id}",
                headers=_headers(),
                params={"delete_remote_artifacts": str(delete_remote_artifacts).lower()},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            return [TextContent(type="text", text=f"Task {thread_id} stopped and cleaned up.")]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _run():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
