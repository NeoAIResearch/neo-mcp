package poller

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"neo-agent/config"
	"neo-agent/executor"
)

type backendCommand map[string]any

func Start() {
	token := config.GetToken()
	serverURL := config.GetServerURL()
	interval := config.GetPollIntervalSeconds()
	deploymentID := config.GetDeploymentID()

	if token == "" {
		fmt.Fprintln(os.Stderr, "poller disabled: missing NEO_TOKEN/NEO_SECRET_KEY")
		return
	}
	if deploymentID == "" {
		fmt.Fprintln(os.Stderr, "poller disabled: missing deployment_id")
		return
	}
	if !config.IsAllowedServerURL(serverURL) {
		fmt.Fprintf(os.Stderr, "poller disabled: insecure NEO_SERVER value: %s\n", serverURL)
		return
	}

	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()

	// Run one immediate cycle.
	pollOnce(token, serverURL, deploymentID)
	for {
		<-ticker.C
		pollOnce(token, serverURL, deploymentID)
	}
}

func pollOnce(token, serverURL, deploymentID string) {
	url := fmt.Sprintf("%s/v2/poll/%s?max_messages=10&wait_time=5", strings.TrimRight(serverURL, "/"), deploymentID)
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := client.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized {
		fmt.Fprintln(os.Stderr, "poller auth rejected (401)")
		return
	}
	if resp.StatusCode != http.StatusOK {
		return
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return
	}

	commands, err := parseCommands(body)
	if err != nil || len(commands) == 0 {
		return
	}

	for _, raw := range commands {
		cmd := toExecutorCommand(raw)
		result := executor.Dispatch(cmd)

		if tid, ok := raw["thread_id"].(string); ok && tid != "" {
			result["thread_id"] = tid
		}
		if q, ok := raw["response_queue_name"].(string); ok && q != "" {
			result["response_queue_name"] = q
		}

		postResult(token, serverURL, deploymentID, result)
	}
}

func parseCommands(body []byte) ([]backendCommand, error) {
	var arr []backendCommand
	if err := json.Unmarshal(body, &arr); err == nil {
		return arr, nil
	}

	var wrap struct {
		Messages []backendCommand `json:"messages"`
	}
	if err := json.Unmarshal(body, &wrap); err != nil {
		return nil, err
	}
	return wrap.Messages, nil
}

func toExecutorCommand(raw backendCommand) executor.Command {
	action, _ := raw["action"].(string)
	requestID, _ := raw["request_id"].(string)
	threadID, _ := raw["thread_id"].(string)
	queue, _ := raw["response_queue_name"].(string)

	payload := map[string]any{}
	if p, ok := raw["payload"].(map[string]any); ok && p != nil {
		for k, v := range p {
			payload[k] = v
		}
	}

	// Flatten top-level command fields into payload for handlers.
	for k, v := range raw {
		if k == "action" || k == "request_id" || k == "thread_id" || k == "response_queue_name" || k == "payload" {
			continue
		}
		if _, exists := payload[k]; !exists {
			payload[k] = v
		}
	}

	if requestID == "" {
		requestID = "go-daemon-request"
	}

	return executor.Command{
		Action:            action,
		RequestID:         requestID,
		ThreadID:          threadID,
		ResponseQueueName: queue,
		Payload:           payload,
	}
}

func postResult(token, serverURL, deploymentID string, result map[string]any) {
	if _, ok := result["sandbox_id"]; !ok {
		result["sandbox_id"] = deploymentID
	}

	payload, _ := json.Marshal(result)
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(serverURL, "/")+"/v2/poll/response", bytes.NewBuffer(payload))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
}
