package singleton

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// AcquireOrExit enforces one Go daemon per deployment.
//
// On entry it:
//  1. Checks whether a daemon with the same deployment_id is already alive; if
//     so it prints a message to stderr and calls os.Exit(0) (not an error —
//     the caller just doesn't need to start another one).
//  2. Kills any other stale go_daemon*.pid processes that match the --daemon
//     cmdline pattern.
//  3. Writes the current process PID atomically to
//     ~/.neo/daemon/go_daemon.pid (and go_daemon_<id[:8]>.pid when a
//     deployment_id is supplied).
//  4. Installs a SIGTERM/SIGINT handler that runs cleanup before exit.
//
// The returned cleanup func removes the PID files; wire it to a defer so the
// files are removed on normal exit too.
func AcquireOrExit(deploymentID string) func() {
	daemonDir := filepath.Join(homeDir(), ".neo", "daemon")
	_ = os.MkdirAll(daemonDir, 0o755)

	genericPID := filepath.Join(daemonDir, "go_daemon.pid")
	var depPID string
	if len(deploymentID) >= 8 {
		depPID = filepath.Join(daemonDir, "go_daemon_"+deploymentID[:8]+".pid")
	}

	// Step 1: check whether an identical daemon is already running.
	checkPath := depPID
	if checkPath == "" {
		checkPath = genericPID
	}
	if pid := readPID(checkPath); pid != 0 && pidAlive(pid) && cmdlineContains(pid, "--daemon") {
		fmt.Fprintf(os.Stderr, "neo-agent daemon already running (pid=%d), exiting\n", pid)
		os.Exit(0)
	}

	// Step 2: kill any other stale go_daemon*.pid processes.
	entries, _ := filepath.Glob(filepath.Join(daemonDir, "go_daemon*.pid"))
	for _, p := range entries {
		pid := readPID(p)
		if pid == 0 {
			_ = os.Remove(p)
			continue
		}
		if !pidAlive(pid) {
			_ = os.Remove(p)
			continue
		}
		if !cmdlineContains(pid, "--daemon") {
			_ = os.Remove(p)
			continue
		}
		// Live daemon — terminate it.
		killProcess(pid)
		time.Sleep(200 * time.Millisecond)
		if pidAlive(pid) {
			killProcessForce(pid)
		}
		_ = os.Remove(p)
	}

	// Step 3: write current PID atomically.
	writePIDatomic(genericPID)
	if depPID != "" {
		writePIDatomic(depPID)
	}

	cleanup := func() {
		_ = os.Remove(genericPID)
		if depPID != "" {
			_ = os.Remove(depPID)
		}
	}

	// Step 4: handle termination signals so PID files are removed on graceful stop.
	installSignalHandler(cleanup)

	// Write discovery files so the Python MCP server can find this daemon's
	// deployment ID via _discover_sandbox_id() without needing port 31337.
	WriteDaemonLog(deploymentID, daemonDir)
	WriteStandaloneDeploymentID(deploymentID, daemonDir)

	return cleanup
}

// WriteDaemonLog appends a {"sandboxId":"<id>","source":"go-daemon"} line to
// ~/.neo/daemon/daemon.log — the same format the Python daemon uses.
func WriteDaemonLog(deploymentID, daemonDir string) {
	if deploymentID == "" {
		return
	}
	entry, _ := json.Marshal(map[string]string{
		"sandboxId": deploymentID,
		"source":    "go-daemon",
	})
	f, err := os.OpenFile(filepath.Join(daemonDir, "daemon.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(entry, '\n'))
}

// WriteStandaloneDeploymentID writes the deployment UUID to
// ~/.neo/daemon/standalone_deployment_id — the fallback discovery path.
func WriteStandaloneDeploymentID(deploymentID, daemonDir string) {
	if deploymentID == "" {
		return
	}
	path := filepath.Join(daemonDir, "standalone_deployment_id")
	_ = os.WriteFile(path, []byte(deploymentID), 0o644)
}

// homeDir returns the current user's home directory, falling back to "/root".
func homeDir() string {
	if h, err := os.UserHomeDir(); err == nil {
		return h
	}
	return "/root"
}

// readPID reads a PID from a file; returns 0 on any error.
func readPID(path string) int {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || pid <= 0 {
		return 0
	}
	return pid
}

// writePIDatomic writes the current PID to path via a temp file + rename.
func writePIDatomic(path string) {
	tmp := path + ".tmp"
	_ = os.WriteFile(tmp, []byte(strconv.Itoa(os.Getpid())), 0o644)
	_ = os.Rename(tmp, path)
}

// LocalIDFile is the path where the daemon's unique local deployment ID is stored.
func LocalIDFile() string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "/root"
	}
	return filepath.Join(home, ".neo", "daemon", "go_daemon_local_id")
}

// EnsureLocalID reads the Go daemon's local unique deployment ID from disk, or
// generates and persists a new one. This ID is independent of the API-key-derived
// deployment ID, so this daemon never competes with the VS Code extension or any
// other daemon that uses the key-derived ID.
func EnsureLocalID() string {
	path := LocalIDFile()
	if b, err := os.ReadFile(path); err == nil {
		uid := strings.TrimSpace(string(b))
		if isUUID(uid) {
			return uid
		}
	}
	uid := newUUID()
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	_ = os.WriteFile(path, []byte(uid), 0o644)
	return uid
}

// isUUID returns true when s is a standard 36-char hyphenated UUID.
func isUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i, c := range s {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if c != '-' {
				return false
			}
		} else if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return true
}

// newUUID returns a random UUID v4.
func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}
