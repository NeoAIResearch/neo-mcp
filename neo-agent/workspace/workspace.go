package workspace

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Manager reads ~/.neo/daemon/thread-workspaces.json and provides a
// thread_id → absolute workspace path lookup.
//
// The JSON file is written by the Python MCP server before task commands
// arrive, so a short staleness TTL (≤ poll interval) is sufficient.
//
// Two JSON value formats are supported:
//
//	Legacy:  {"thread_id": "/abs/path"}
//	Current: {"thread_id": {"workspace": "/abs/path", "updated_at": 1234}}
type Manager struct {
	mu       sync.RWMutex
	entries  map[string]string
	jsonPath string
	lastLoad time.Time
}

// New returns a Manager loaded from the standard thread-workspaces.json.
// Errors during the initial load are silently ignored (empty map is fine).
func New() *Manager {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "/root"
	}
	m := &Manager{
		entries:  map[string]string{},
		jsonPath: filepath.Join(home, ".neo", "daemon", "thread-workspaces.json"),
	}
	_ = m.reload()
	return m
}

// Get returns the workspace path for threadID, or "" if not found.
func (m *Manager) Get(threadID string) string {
	if threadID == "" {
		return ""
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.entries[threadID]
}

// ReloadIfStale re-reads the JSON file when more than ttl has elapsed since
// the last successful load.
func (m *Manager) ReloadIfStale(ttl time.Duration) {
	m.mu.RLock()
	stale := time.Since(m.lastLoad) > ttl
	m.mu.RUnlock()
	if stale {
		_ = m.reload()
	}
}

// reload unconditionally re-reads the JSON file and updates the in-memory map.
func (m *Manager) reload() error {
	b, err := os.ReadFile(m.jsonPath)
	if err != nil {
		return err
	}

	// The JSON is a flat object; values can be strings or nested objects.
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}

	next := make(map[string]string, len(raw))
	for tid, v := range raw {
		// Try string first (legacy format).
		var s string
		if json.Unmarshal(v, &s) == nil {
			next[tid] = s
			continue
		}
		// Try nested object {"workspace": "...", ...}.
		var obj struct {
			Workspace string `json:"workspace"`
		}
		if json.Unmarshal(v, &obj) == nil && obj.Workspace != "" {
			next[tid] = obj.Workspace
		}
	}

	m.mu.Lock()
	m.entries = next
	m.lastLoad = time.Now()
	m.mu.Unlock()
	return nil
}
