import os
import httpx

NEO_API_KEY = os.environ.get("NEO_API_KEY", "")
NEO_SECRET_KEY = os.environ.get("NEO_SECRET_KEY", "")
if not NEO_API_KEY or not NEO_SECRET_KEY:
    print("ERROR: NEO_API_KEY and NEO_SECRET_KEY environment variables are required.")
    raise SystemExit(1)

url = "https://master.heyneo.so/v2/thread/init-chat-direct"
headers = {
    "Authorization": f"Bearer {NEO_SECRET_KEY}",
    "x-access-key": NEO_API_KEY,
}
body = {"message": "list files in workspace", "deployment_type": "vscode"}

resp = httpx.post(url, headers=headers, json=body, timeout=30)
print(f"HTTP {resp.status_code}")
print(resp.text)

if resp.status_code == 200:
    data = resp.json()
    thread_id = data.get("thread_id", data.get("id", "unknown"))
    print(f"Connection OK — thread_id: {thread_id}")
else:
    print(f"Error {resp.status_code}: {resp.text}")
