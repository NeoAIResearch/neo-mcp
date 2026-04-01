FROM python:3.11-slim

WORKDIR /app

COPY python/pyproject.toml .
COPY python/requirements.txt .
COPY python/src/ src/

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

# HTTP mode is currently disabled — this image runs in stdio mode only.
# Use: docker run -i --rm -e NEO_SECRET_KEY=sk-v1-... neo-mcp
ENTRYPOINT ["neo-mcp"]
