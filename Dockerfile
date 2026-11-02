# OpsAgents for hosting (Render, or any Docker host).
#
#   docker build -t opsagents .
#   docker run -p 8080:8080 -e PORT=8080 -e DATABASE_URL=postgresql://... --env-file .env opsagents
#
# The database is external (PostgreSQL 16 with pgvector, e.g. Render Postgres); OPA runs
# inside the container next to the app. OpenTofu is included so the Dependency Resolver
# reads the real `tofu graph` instead of its static fallback.
FROM python:3.12-slim

ARG OPA_VERSION=1.20.2
ARG TOFU_VERSION=1.9.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    PORT=8080

# git: Merge Assistant and Code Reviewer. OPA: the guardrail policy. OpenTofu: dependency graphs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && curl -fsSL -o /usr/local/bin/opa \
      "https://github.com/open-policy-agent/opa/releases/download/v${OPA_VERSION}/opa_linux_amd64_static" \
 && chmod 755 /usr/local/bin/opa \
 && curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin tofu \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Packages first, so code changes don't reinstall them. The test-only packages are left out.
COPY requirements.txt .
RUN grep -vE '^(pgserver|pytest|iniconfig|pluggy)==' requirements.txt > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

# The knowledge base's embedding model, downloaded once at build time instead of on every start.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

COPY . .
RUN pip install --no-deps -e . \
 && useradd --create-home --uid 10001 opsagents \
 && chown -R opsagents /app /opt/fastembed
USER opsagents
RUN git config --global user.name "OpsAgents" \
 && git config --global user.email "opsagents@localhost" \
 && git config --global init.defaultBranch main

EXPOSE 8080
CMD ["python", "-m", "app", "--cloud", "--no-browser"]
