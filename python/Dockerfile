FROM golang:1.22 AS go-builder

WORKDIR /src/neo-agent
COPY neo-agent/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/neo-agent .

FROM python:3.11-slim

WORKDIR /app

COPY python/pyproject.toml .
COPY python/requirements.txt .
COPY python/src/ src/

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

RUN mkdir -p /root/.neo /root/.neo/daemon
COPY --from=go-builder /out/neo-agent /root/.neo/agent
RUN chmod +x /root/.neo/agent

# HTTP transport port (used when NEO_TRANSPORT=http)
EXPOSE 8000


ENV NEO_TRANSPORT=http
ENV NEO_HTTP_HOST=0.0.0.0
ENV NEO_GO_AGENT_BIN=/root/.neo/agent

ENTRYPOINT ["neo-mcp"]
