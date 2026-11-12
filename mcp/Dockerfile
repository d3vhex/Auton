# Auton agent HTTP service image (built locally, imported into k3d).
FROM python:3.11-slim

# git      -> git_status / git_diff
# ripgrep  -> fast, gitignore-aware grep/glob (Python fallback exists)
# bwrap    -> optional AGENT_SANDBOX=bwrap for host CLI runs
# curl     -> used below to fetch kubectl
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep bubblewrap ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# kubectl for the Kubernetes tools. Pin the minor to the cluster server
# (kubectl supports +/-1 minor). Check with: kubectl version
ARG KUBECTL_VERSION=v1.35.5
RUN set -eu; \
    arch="$(uname -m)"; \
    case "$arch" in \
      x86_64) arch=amd64 ;; \
      aarch64|arm64) arch=arm64 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    base="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}"; \
    curl -fsSL "$base/kubectl" -o /usr/local/bin/kubectl; \
    curl -fsSL "$base/kubectl.sha256" -o /tmp/kubectl.sha256; \
    echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod +x /usr/local/bin/kubectl; \
    rm -f /tmp/kubectl.sha256

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# NOTE: keep this list in sync with the modules in mcp/ (AGENTS.md).
COPY agent_core.py agent_server.py agent_client.py agent_service.py \
     audit.py incidents.py k8s_tools.py llm.py n8n_tools.py \
     playbooks.py ops_controller.py ./
COPY ui ./ui

ENV AGENT_WORKSPACE=/workspace \
    AGENT_PORT=8080 \
    AGENT_BIND=0.0.0.0 \
    OLLAMA_HOST=http://ollama-service:11434 \
    AGENT_MODEL=qwen2.5-coder:7b-instruct \
    AGENT_NUM_CTX=16384 \
    AGENT_MAX_QUEUE=8 \
    AGENT_JOB_TIMEOUT=900 \
    AGENT_SANDBOX=off \
    AGENT_NAMESPACE=auton \
    AGENT_K8S_ENABLED=1 \
    AGENT_K8S_WRITE=0 \
    N8N_API_URL=http://n8n:5678/api/v1 \
    N8N_WEBHOOK_BASE=http://n8n:5678/webhook \
    N8N_API_KEY_FILE=/etc/n8n-api/api-key \
    AGENT_RUN_URL=http://auton:8080/run
RUN mkdir -p /workspace

EXPOSE 8080
CMD ["python", "agent_service.py"]
