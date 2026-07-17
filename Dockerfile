FROM python:3.12-slim

WORKDIR /app

# Bundle the self-hosted GitHub MCP server so GitHub actions are attributed to
# the App bot (via the per-request installation token). Runs as a stdio subprocess.
ARG GITHUB_MCP_VERSION=1.5.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL -o /tmp/ghmcp.tgz \
        "https://github.com/github/github-mcp-server/releases/download/v${GITHUB_MCP_VERSION}/github-mcp-server_Linux_x86_64.tar.gz" \
    && tar -xzf /tmp/ghmcp.tgz -C /usr/local/bin github-mcp-server \
    && rm /tmp/ghmcp.tgz \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
# Use the bundled binary (attributes writes to the App bot) by default.
ENV GITHUB_MCP_MODE=local

COPY . user_agent/
WORKDIR /app/user_agent

RUN pip install --no-input --upgrade pip && \
    if [ -f requirements.txt ]; then \
    pip install --no-input -r requirements.txt; \
    else \
    echo "No requirements.txt found"; \
    fi

EXPOSE 8088

CMD ["python", "main.py"]

