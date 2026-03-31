VERSION ?= 1.0.0

.PHONY: build build-all clean

build:
	go build -o neo-agent .

build-all:
	mkdir -p releases
	GOOS=darwin  GOARCH=arm64 go build -o releases/neo-agent-mac .
	GOOS=darwin  GOARCH=amd64 go build -o releases/neo-agent-mac-intel .
	GOOS=linux   GOARCH=amd64 go build -o releases/neo-agent-linux .
	GOOS=linux   GOARCH=arm64 go build -o releases/neo-agent-linux-arm .
	GOOS=windows GOARCH=amd64 go build -o releases/neo-agent.exe .

clean:
	rm -rf releases neo-agent
