FROM node:20-slim

WORKDIR /app

# No runtime npm deps — daemon uses only Node.js built-ins
COPY dist/ dist/
COPY bin/ bin/
COPY package.json .

# Mount project workspace and Neo config dir
VOLUME ["/workspace", "/root/.neo"]

# Required: NEO_SECRET_KEY=sk-v1-...
# Optional: NEO_ENV=staging|prod (default: prod)
#           NEO_API_URL=https://... (explicit override)

ENTRYPOINT ["node", "bin/test-neo-mcp-daemon", "/workspace"]
