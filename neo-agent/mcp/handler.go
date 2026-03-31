package mcp

import (
	"fmt"

	"neo-agent/config"
	"neo-agent/executor"
)

func Handle(req map[string]any) map[string]any {
	id := req["id"]
	method, _ := req["method"].(string)

	switch method {
	case "initialize":
		return ok(id, map[string]any{
			"protocolVersion": "2024-11-05",
			"serverInfo": map[string]string{
				"name":    "neo-agent",
				"version": "1.0.0",
			},
			"capabilities": map[string]any{
				"tools": map[string]bool{"listChanged": false},
			},
		})

	case "tools/list":
		return ok(id, map[string]any{"tools": tools()})

	case "tools/call":
		params, _ := req["params"].(map[string]any)
		if params == nil {
			return errResp(id, -32602, "Invalid params")
		}
		name, _ := params["name"].(string)
		args, _ := params["arguments"].(map[string]any)
		if args == nil {
			args = map[string]any{}
		}

		if name == "neo_info" {
			text := fmt.Sprintf("server=%s workspace=%s deployment_id=%s", config.GetServerURL(), config.GetWorkspaceRoot(), config.GetDeploymentID())
			return toolText(id, text)
		}

		out := executor.Execute(name, args)
		return toolText(id, out)

	case "ping":
		return ok(id, map[string]any{})
	}

	return errResp(id, -32601, "Method not found")
}

func tools() []map[string]any {
	return []map[string]any{
		{
			"name":        "create_file",
			"description": "Create a file under the allowed local workspace",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path":    map[string]string{"type": "string", "description": "Path relative to workspace"},
					"content": map[string]string{"type": "string", "description": "File content"},
				},
				"required": []string{"path", "content"},
			},
		},
		{
			"name":        "read_file",
			"description": "Read a file under the allowed local workspace",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path": map[string]string{"type": "string"},
				},
				"required": []string{"path"},
			},
		},
		{
			"name":        "run_file",
			"description": "Execute a script file under the allowed local workspace",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path": map[string]string{"type": "string"},
				},
				"required": []string{"path"},
			},
		},
		{
			"name":        "run_command",
			"description": "Run a shell command with working directory set to allowed workspace",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"command": map[string]string{"type": "string"},
				},
				"required": []string{"command"},
			},
		},
		{
			"name":        "delete_file",
			"description": "Delete a file under the allowed local workspace",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path": map[string]string{"type": "string"},
				},
				"required": []string{"path"},
			},
		},
		{
			"name":        "neo_info",
			"description": "Show runtime info (server URL, workspace, deployment ID)",
			"inputSchema": map[string]any{
				"type":       "object",
				"properties": map[string]any{},
				"required":   []string{},
			},
		},
	}
}

func ok(id any, result any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "result": result}
}

func errResp(id any, code int, message string) map[string]any {
	return map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"error": map[string]any{
			"code":    code,
			"message": message,
		},
	}
}

func toolText(id any, text string) map[string]any {
	return ok(id, map[string]any{
		"content": []map[string]string{{"type": "text", "text": text}},
	})
}
