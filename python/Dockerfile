FROM python:3.11-slim

WORKDIR /app

COPY python/pyproject.toml .
COPY python/requirements.txt .
COPY python/src/ src/

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir . \
    && adduser --disabled-password --gecos "" --uid 1000 neo \
    && mkdir -p /tmp/neo-workspace /home/neo/.neo \
    && chown -R neo:neo /tmp/neo-workspace /home/neo/.neo

# Security: run as non-root user
USER neo

# Constrain the daemon workspace to /tmp — prevents the deployed HTTP bridge from
# writing files outside the container's temp directory.
# NEO_NO_DAEMON disables the background Python poller (no local execution in HTTP bridge mode).
ENV NEO_WORKSPACE_DIR=/tmp/neo-workspace \
    NEO_NO_DAEMON=true \
    HOME=/home/neo

ENTRYPOINT ["neo-mcp"]
