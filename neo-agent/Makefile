VERSION ?= 1.0.0

# npm package root (relative to this Makefile)
NPM_DIR := ../npm/binaries

.PHONY: build build-all build-npm clean

build:
	go build -o neo-agent .

build-all:
	mkdir -p releases
	GOOS=darwin  GOARCH=arm64 go build -o releases/neo-agent-mac .
	GOOS=darwin  GOARCH=amd64 go build -o releases/neo-agent-mac-intel .
	GOOS=linux   GOARCH=amd64 go build -o releases/neo-agent-linux .
	GOOS=linux   GOARCH=arm64 go build -o releases/neo-agent-linux-arm .
	GOOS=windows GOARCH=amd64 go build -o releases/neo-agent.exe .

# build-npm: build all platforms + copy into npm package binaries/
build-npm: build-all
	mkdir -p $(NPM_DIR)
	cp releases/neo-agent-mac       $(NPM_DIR)/neo-agent-mac
	cp releases/neo-agent-mac-intel $(NPM_DIR)/neo-agent-mac-intel
	cp releases/neo-agent-linux     $(NPM_DIR)/neo-agent-linux
	cp releases/neo-agent-linux-arm $(NPM_DIR)/neo-agent-linux-arm
	cp releases/neo-agent.exe       $(NPM_DIR)/neo-agent.exe

clean:
	rm -rf releases neo-agent
