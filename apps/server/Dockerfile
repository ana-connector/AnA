FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/ packages/
COPY apps/server/ apps/server/
RUN pip install --no-cache-dir -r apps/server/requirements.txt

ENV ANA_WORKSPACE_DIR=/home/hello/workspace
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.server.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
