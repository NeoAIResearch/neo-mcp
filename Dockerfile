FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY requirements.txt .
COPY src/ src/

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

# HTTP transport port (used when NEO_TRANSPORT=http)
EXPOSE 8000

ENV NEO_HTTP_HOST=0.0.0.0

ENTRYPOINT ["neo-mcp"]
