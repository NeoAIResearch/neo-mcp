package config

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const (
	defaultServerURL    = "https://master.heyneo.so"
	defaultPollInterval = 5
)

func GetToken() string {
	if t := strings.TrimSpace(os.Getenv("NEO_TOKEN")); t != "" {
		return t
	}

	if t := strings.TrimSpace(os.Getenv("NEO_SECRET_KEY")); t != "" {
		return t
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}

	path := filepath.Join(home, ".neo", "config")
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()

	s := bufio.NewScanner(f)
	for s.Scan() {
		line := strings.TrimSpace(s.Text())
		if strings.HasPrefix(line, "token=") {
			return strings.TrimSpace(strings.TrimPrefix(line, "token="))
		}
	}
	return ""
}

func GetServerURL() string {
	if u := strings.TrimSpace(os.Getenv("NEO_API_URL")); u != "" {
		return strings.TrimRight(u, "/")
	}
	if u := strings.TrimSpace(os.Getenv("NEO_SERVER")); u != "" {
		return strings.TrimRight(u, "/")
	}
	return defaultServerURL
}

func IsAllowedServerURL(url string) bool {
	lower := strings.ToLower(strings.TrimSpace(url))
	if strings.HasPrefix(lower, "https://") {
		return true
	}
	if strings.HasPrefix(lower, "http://127.0.0.1") || strings.HasPrefix(lower, "http://localhost") {
		return true
	}
	return false
}

func GetPollIntervalSeconds() int {
	raw := strings.TrimSpace(os.Getenv("NEO_POLL_INTERVAL"))
	if raw == "" {
		return defaultPollInterval
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return defaultPollInterval
	}
	return n
}

func GetWorkspaceRoot() string {
	if w := strings.TrimSpace(os.Getenv("NEO_WORKSPACE")); w != "" {
		abs, err := filepath.Abs(w)
		if err == nil {
			return abs
		}
		return w
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return "."
	}
	return filepath.Join(home, "neo-workspace")
}

func GetDeploymentID() string {
	if dep := strings.TrimSpace(os.Getenv("NEO_DEPLOYMENT_ID")); dep != "" {
		return dep
	}

	token := GetToken()
	if token == "" {
		return ""
	}

	sum := sha256.Sum256([]byte(token))
	// UUID-ish stable ID from SHA-256 first 16 bytes.
	b := sum[:16]
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex.EncodeToString(b[0:4]),
		hex.EncodeToString(b[4:6]),
		hex.EncodeToString(b[6:8]),
		hex.EncodeToString(b[8:10]),
		hex.EncodeToString(b[10:16]),
	)
}
