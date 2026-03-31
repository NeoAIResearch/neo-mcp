package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"neo-agent/mcp"
	"neo-agent/poller"
)

func main() {
	daemonOnly := flag.Bool("daemon", false, "run as background daemon poller only")
	flag.Parse()

	if *daemonOnly {
		poller.Start()
		return
	}

	go poller.Start()

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	for scanner.Scan() {
		line := scanner.Bytes()

		var req map[string]any
		if err := json.Unmarshal(line, &req); err != nil {
			_ = writeJSON(map[string]any{
				"jsonrpc": "2.0",
				"id":      nil,
				"error": map[string]any{
					"code":    -32700,
					"message": "Parse error",
				},
			})
			continue
		}

		resp := mcp.Handle(req)
		if err := writeJSON(resp); err != nil {
			fmt.Fprintf(os.Stderr, "failed to write response: %v\n", err)
		}
	}

	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "stdin scan error: %v\n", err)
	}
}

func writeJSON(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(append(b, '\n'))
	return err
}
