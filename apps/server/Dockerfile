FROM python:3.13-slim

# docker-cli (the client only — `docker.io` on Debian 13 ships the daemon and
# not /usr/bin/docker). Used only when per-workspace containers are switched
# on, but installing it here keeps that a runtime decision rather than an
# image rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git docker-cli \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    # Pinned, not floating: a cached layer kept the image on claude 2.1.197
    # long after the account needed 2.1.280 ("Claude Code 2.1.197 does not
    # support this model"), and a bare `npm install -g` gives no way to tell
    # which version an image actually holds. Bump these deliberately.
    && npm install -g @anthropic-ai/claude-code@2.1.280 @openai/codex@0.156.1 \
    # `uv`, for `uvx`: the MCP servers in config/mcp.yaml are Python ones run
    # straight from PyPI, and without uvx on PATH they are skipped as
    # unavailable rather than failing loudly.
    && curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/ packages/
COPY apps/server/ apps/server/
# The governance rule set the gate evaluates every tool call against. Without
# it the gate raises and every call is denied fail-closed, which looks like a
# broken agent rather than a missing file.
COPY config/ config/
RUN pip install --no-cache-dir -r apps/server/requirements.txt

# The `claude` CLI refuses to run at all as root when auto-approval is on
# ("--dangerously-skip-permissions cannot be used with root/sudo privileges
# for security reasons"), so the app runs as an unprivileged user. The uid/gid
# default to 1000 to match the host user that owns the mounted workspace and
# CLI config files — a mismatch makes those mounts read-only in practice.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} ana \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -s /bin/bash ana \
    # Created here, owned by ana, so the named volume mounted over it
    # inherits that ownership from the image — an empty volume would
    # otherwise come up root-owned and the fastembed model download fails
    # with "Permission denied (os error 13)".
    # Both are mount points for named volumes: an empty volume inherits the
    # ownership of the directory in the image, and a root-owned one makes the
    # CLI settings file unwritable ("Permission denied: .ana/claude_settings.json").
    && mkdir -p /home/ana/.cache /home/ana/.ana && chown ana:ana /home/ana/.cache /home/ana/.ana
USER ana
ENV HOME=/home/ana

ENV ANA_WORKSPACE_DIR=/home/hello/workspace
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.server.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
