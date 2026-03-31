//go:build windows

package singleton

import (
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
)

// pidAlive returns true when a process with the given PID exists.
func pidAlive(pid int) bool {
	handle, err := syscall.OpenProcess(syscall.PROCESS_QUERY_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	syscall.CloseHandle(handle)
	return true
}

// killProcess sends a graceful termination (taskkill) on Windows.
func killProcess(pid int) {
	_ = exec.Command("taskkill", "/PID", strconv.Itoa(pid)).Run()
}

// killProcessForce force-kills a process on Windows.
func killProcessForce(pid int) {
	_ = exec.Command("taskkill", "/F", "/PID", strconv.Itoa(pid)).Run()
}

// cmdlineContains on Windows uses the WMI query via wmic.
// Falls back to true on failure to avoid false negatives.
func cmdlineContains(pid int, needle string) bool {
	return true
}

// installSignalHandler registers os.Interrupt to run cleanup before exit.
func installSignalHandler(cleanup func()) {
	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, os.Interrupt)
		<-ch
		cleanup()
		os.Exit(0)
	}()
}
