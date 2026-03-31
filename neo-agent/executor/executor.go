package executor

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"neo-agent/config"
	"neo-agent/workspace"
)

var wsMgr *workspace.Manager

// Init wires the workspace manager used for per-thread path resolution.
// Call once from main before any requests are dispatched.
func Init(m *workspace.Manager) {
	wsMgr = m
}

type Command struct {
	Action            string         `json:"action"`
	RequestID         string         `json:"request_id"`
	ThreadID          string         `json:"thread_id,omitempty"`
	ResponseQueueName string         `json:"response_queue_name,omitempty"`
	Payload           map[string]any `json:"payload,omitempty"`
}

type Job struct {
	cmd      *exec.Cmd
	stdout   strings.Builder
	stderr   strings.Builder
	exitCode *int
	mu       sync.Mutex
}

var (
	jobsMu sync.Mutex
	jobs   = map[string]*Job{}
)

var skipDirs = map[string]bool{
	"venv": true, "node_modules": true, "env": true, ".venv": true,
	"__pycache__": true, ".git": true, ".tox": true, "dist": true, "build": true,
}

func Execute(action string, args map[string]any) string {
	cmd := Command{Action: action, RequestID: "tool-call", Payload: args}
	result := Dispatch(cmd)
	if errStr, ok := result["error"].(string); ok && errStr != "" {
		return "Error: " + errStr
	}
	if data, ok := result["data"].(map[string]any); ok {
		if out, ok := data["stdout"].(string); ok {
			return out
		}
		if msg, ok := data["message"].(string); ok {
			return msg
		}
		if fc, ok := data["file_content"].(string); ok {
			return fc
		}
	}
	return fmt.Sprintf("%v", result)
}

func Dispatch(cmd Command) map[string]any {
	switch cmd.Action {
	case "create_session":
		return map[string]any{
			"request_id": cmd.RequestID,
			"status":     "success",
			"data":       map[string]any{"coding_session_id": fieldString(cmd, "session_id", "session")},
		}
	case "write_code":
		return hWriteCode(cmd)
	case "get_file":
		return hGetFile(cmd)
	case "run_subprocess":
		return hRunSubprocess(cmd)
	case "get_job_status":
		return hGetJobStatus(cmd)
	case "terminate_job":
		return hTerminateJob(cmd)
	case "list_files":
		return hListFiles(cmd)

	// direct MCP convenience aliases
	case "create_file":
		return hWriteCode(Command{Action: "write_code", RequestID: cmd.RequestID, Payload: map[string]any{"filename": fieldString(cmd, "path", ""), "code": fieldString(cmd, "content", "")}})
	case "read_file":
		return hGetFile(Command{Action: "get_file", RequestID: cmd.RequestID, Payload: map[string]any{"file_path": fieldString(cmd, "path", "")}})
	case "run_file":
		return hRunSubprocess(Command{Action: "run_subprocess", RequestID: cmd.RequestID, Payload: map[string]any{"command": "sh " + shellQuote(fieldString(cmd, "path", "")), "detach": false}})
	case "run_command":
		return hRunSubprocess(Command{Action: "run_subprocess", RequestID: cmd.RequestID, Payload: map[string]any{"command": fieldString(cmd, "command", ""), "detach": false}})
	case "delete_file":
		return hDeleteFile(cmd)

	default:
		return map[string]any{"request_id": cmd.RequestID, "status": "error", "error": "Unknown action: " + cmd.Action}
	}
}

func hWriteCode(cmd Command) map[string]any {
	filename := fieldString(cmd, "filename", "")
	code := fieldString(cmd, "code", "")
	if filename == "" {
		return fail(cmd.RequestID, "filename is required")
	}

	full, e := resolvePath(filename, cmd.ThreadID)
	if e != nil {
		return fail(cmd.RequestID, e.Error())
	}

	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return fail(cmd.RequestID, err.Error())
	}
	if err := os.WriteFile(full, []byte(code), 0o644); err != nil {
		return fail(cmd.RequestID, err.Error())
	}

	return ok(cmd.RequestID, map[string]any{
		"file_path": full,
		"message":   "File written",
		"compile_check": map[string]any{
			"performed": false,
			"success":   true,
			"error":     nil,
		},
	})
}

func hGetFile(cmd Command) map[string]any {
	fp := fieldString(cmd, "file_path", "")
	if fp == "" {
		return fail(cmd.RequestID, "file_path is required")
	}
	full, e := resolvePath(fp, cmd.ThreadID)
	if e != nil {
		return fail(cmd.RequestID, e.Error())
	}
	b, e := os.ReadFile(full)
	if e != nil {
		return fail(cmd.RequestID, e.Error())
	}
	return ok(cmd.RequestID, map[string]any{"file_path": full, "file_content": string(b)})
}

func hDeleteFile(cmd Command) map[string]any {
	p := fieldString(cmd, "path", "")
	if p == "" {
		return fail(cmd.RequestID, "path is required")
	}
	full, e := resolvePath(p, cmd.ThreadID)
	if e != nil {
		return fail(cmd.RequestID, e.Error())
	}
	if e := os.Remove(full); e != nil {
		return fail(cmd.RequestID, e.Error())
	}
	return ok(cmd.RequestID, map[string]any{"message": "Deleted", "file_path": full})
}

func hRunSubprocess(cmd Command) map[string]any {
	command := fieldString(cmd, "command", "")
	if strings.TrimSpace(command) == "" {
		return fail(cmd.RequestID, "command is required")
	}
	detach := fieldBool(cmd, "detach", true)
	workdir := fieldString(cmd, "workdir", "")
	cwd, e := resolveDir(workdir, cmd.ThreadID)
	if e != nil {
		return fail(cmd.RequestID, e.Error())
	}
	_ = os.MkdirAll(cwd, 0o755)

	shCmd := exec.Command("sh", "-lc", command)
	shCmd.Dir = cwd

	if !detach {
		out, e := shCmd.CombinedOutput()
		status := "completed"
		resp := map[string]any{
			"detached":  false,
			"completed": true,
			"exit_code": exitCode(e),
			"stdout":    string(out),
			"stderr":    "",
		}
		if e != nil {
			status = "error"
			return map[string]any{"request_id": cmd.RequestID, "status": status, "error": e.Error(), "data": resp}
		}
		return map[string]any{"request_id": cmd.RequestID, "status": status, "data": resp}
	}

	stdoutPipe, _ := shCmd.StdoutPipe()
	stderrPipe, _ := shCmd.StderrPipe()
	if e := shCmd.Start(); e != nil {
		return fail(cmd.RequestID, e.Error())
	}

	jobID := randID()
	jb := &Job{cmd: shCmd}
	jobsMu.Lock()
	jobs[jobID] = jb
	jobsMu.Unlock()

	go func() {
		b, _ := ioReadAll(stdoutPipe)
		jb.mu.Lock()
		jb.stdout.WriteString(string(b))
		jb.mu.Unlock()
	}()
	go func() {
		b, _ := ioReadAll(stderrPipe)
		jb.mu.Lock()
		jb.stderr.WriteString(string(b))
		jb.mu.Unlock()
	}()
	go func() {
		e := shCmd.Wait()
		code := exitCode(e)
		jb.mu.Lock()
		jb.exitCode = &code
		jb.mu.Unlock()
	}()

	return ok(cmd.RequestID, map[string]any{"job_id": jobID, "detached": true, "message": "Job started"})
}

func hGetJobStatus(cmd Command) map[string]any {
	jobID := fieldString(cmd, "job_id", "")
	if jobID == "" {
		return fail(cmd.RequestID, "job_id is required")
	}
	jobsMu.Lock()
	jb := jobs[jobID]
	jobsMu.Unlock()
	if jb == nil {
		return fail(cmd.RequestID, "Job not found: "+jobID)
	}

	jb.mu.Lock()
	defer jb.mu.Unlock()
	completed := jb.exitCode != nil
	status := "pending"
	if completed {
		status = "completed"
	}
	exit := any(nil)
	if jb.exitCode != nil {
		exit = *jb.exitCode
	}

	return map[string]any{
		"request_id": cmd.RequestID,
		"status":     status,
		"data": map[string]any{
			"job_id":    jobID,
			"stdout":    jb.stdout.String(),
			"stderr":    jb.stderr.String(),
			"exit_code": exit,
			"completed": completed,
		},
	}
}

func hTerminateJob(cmd Command) map[string]any {
	jobID := fieldString(cmd, "job_id", "")
	if jobID == "" {
		return fail(cmd.RequestID, "job_id is required")
	}
	jobsMu.Lock()
	jb := jobs[jobID]
	jobsMu.Unlock()
	if jb == nil {
		return fail(cmd.RequestID, "Job not found: "+jobID)
	}
	_ = jb.cmd.Process.Kill()
	code := -15
	jb.mu.Lock()
	jb.exitCode = &code
	jb.stderr.WriteString("\n[terminated by daemon]")
	jb.mu.Unlock()

	return ok(cmd.RequestID, map[string]any{"job_id": jobID, "terminated": true})
}

func hListFiles(cmd Command) map[string]any {
	dir := fieldString(cmd, "directory", "")
	maxDepth := fieldInt(cmd, "max_depth", 10)
	includeHidden := fieldBool(cmd, "include_hidden", false)

	start, e := resolveDir(dir, cmd.ThreadID)
	if e != nil {
		return fail(cmd.RequestID, e.Error())
	}

	lines := []string{start + "|d|0"}
	var walk func(string, int)
	walk = func(cur string, depth int) {
		if depth > maxDepth {
			return
		}
		entries, e := os.ReadDir(cur)
		if e != nil {
			return
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, ent := range entries {
			name := ent.Name()
			if !includeHidden && strings.HasPrefix(name, ".") {
				continue
			}
			full := filepath.Join(cur, name)
			if ent.IsDir() {
				lines = append(lines, full+"|d|0")
				if !skipDirs[name] {
					walk(full, depth+1)
				}
				continue
			}
			info, e := ent.Info()
			size := int64(0)
			if e == nil {
				size = info.Size()
			}
			lines = append(lines, fmt.Sprintf("%s|f|%d", full, size))
		}
	}
	walk(start, 1)

	return ok(cmd.RequestID, map[string]any{"stdout": strings.Join(lines, "\n"), "file_count": len(lines), "directory": start})
}

func ok(requestID string, data map[string]any) map[string]any {
	return map[string]any{"request_id": requestID, "status": "success", "data": data}
}

func fail(requestID, message string) map[string]any {
	return map[string]any{"request_id": requestID, "status": "error", "error": message}
}

func fieldString(cmd Command, key, fallback string) string {
	if v, ok := cmd.Payload[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return fallback
}

func fieldBool(cmd Command, key string, fallback bool) bool {
	if v, ok := cmd.Payload[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return fallback
}

func fieldInt(cmd Command, key string, fallback int) int {
	if v, ok := cmd.Payload[key]; ok {
		switch t := v.(type) {
		case float64:
			return int(t)
		case int:
			return t
		}
	}
	return fallback
}

// workspaceRoot returns the effective workspace root for a given thread.
// If threadID is non-empty and the workspace manager has a mapping, that path
// is used; otherwise it falls back to the NEO_WORKSPACE env / config default.
func workspaceRoot(threadID string) string {
	if threadID != "" && wsMgr != nil {
		wsMgr.ReloadIfStale(30 * time.Second)
		if ws := wsMgr.Get(threadID); ws != "" {
			return ws
		}
	}
	return config.GetWorkspaceRoot()
}

func resolvePath(p string, threadID string) (string, error) {
	p = strings.TrimSpace(p)
	if p == "" {
		return "", fmt.Errorf("empty path")
	}

	if strings.HasPrefix(p, "~/") {
		home, e := os.UserHomeDir()
		if e != nil {
			return "", e
		}
		p = filepath.Join(home, strings.TrimPrefix(p, "~/"))
	}

	root := workspaceRoot(threadID)
	clean := filepath.Clean(p)
	if !filepath.IsAbs(clean) {
		clean = filepath.Join(root, clean)
	}
	abs, e := filepath.Abs(clean)
	if e != nil {
		return "", e
	}
	rootAbs, e := filepath.Abs(root)
	if e != nil {
		return "", e
	}
	rel, e := filepath.Rel(rootAbs, abs)
	if e != nil {
		return "", e
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		// /tmp (and similar temp dirs) is allowed as-is — the backend writes temp
		// scripts there and immediately runs them from the same path.
		if strings.HasPrefix(abs, "/tmp/") || abs == "/tmp" {
			return abs, nil
		}

		// Path is outside workspace — remap by finding workspace base directory
		// name in the path. This handles the case where the Neo backend runs in
		// a container with a different root (e.g. /app/project/foo) while the
		// user's workspace is /root/foo: we strip the foreign prefix and keep
		// only the path components that come after the workspace directory name.
		wsBase := filepath.Base(rootAbs)
		parts := strings.Split(filepath.ToSlash(abs), "/")
		remapped := false
		for i := len(parts) - 1; i >= 0; i-- {
			if parts[i] == wsBase {
				relParts := parts[i+1:]
				if len(relParts) == 0 {
					abs = rootAbs
				} else {
					abs = filepath.Join(append([]string{rootAbs}, relParts...)...)
				}
				remapped = true
				break
			}
		}
		if !remapped {
			// Last resort: use only the base filename inside the workspace.
			abs = filepath.Join(rootAbs, filepath.Base(p))
		}
	}
	return abs, nil
}

func resolveDir(p string, threadID string) (string, error) {
	if strings.TrimSpace(p) == "" {
		return workspaceRoot(threadID), nil
	}
	abs, e := resolvePath(p, threadID)
	if e != nil {
		return "", e
	}
	fi, e := os.Stat(abs)
	if e == nil && fi.IsDir() {
		return abs, nil
	}
	// If path does not exist yet, treat as directory candidate inside workspace
	return abs, nil
}

func shellQuote(s string) string {
	s = strings.ReplaceAll(s, "'", "'\\''")
	return "'" + s + "'"
}

func exitCode(e error) int {
	if e == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(e, &ee) {
		return ee.ExitCode()
	}
	return -1
}

func randID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func ioReadAll(r io.Reader) ([]byte, error) {
	return io.ReadAll(r)
}
