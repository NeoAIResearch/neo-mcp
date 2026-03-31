//go:build !windows

package singleton

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
)

// pidAlive returns true when a process with the given PID exists.
func pidAlive(pid int) bool {
	return syscall.Kill(pid, 0) == nil
}

// killProcess sends SIGTERM to a process.
func killProcess(pid int) {
	_ = syscall.Kill(pid, syscall.SIGTERM)
}

// killProcessForce sends SIGKILL to a process.
func killProcessForce(pid int) {
	_ = syscall.Kill(pid, syscall.SIGKILL)
}

// cmdlineContains returns true when /proc/{pid}/cmdline contains needle.
// Falls back to true on platforms where /proc is unavailable (e.g. macOS).
func cmdlineContains(pid int, needle string) bool {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil {
		// /proc not available — assume it matches to avoid false negatives.
		return true
	}
	cmdline := strings.ReplaceAll(string(b), "\x00", " ")
	return strings.Contains(cmdline, needle)
}

// installSignalHandler registers SIGTERM/SIGINT to run cleanup before exit.
func installSignalHandler(cleanup func()) {
	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGTERM, syscall.SIGINT)
		<-ch
		cleanup()
		os.Exit(0)
	}()
}
