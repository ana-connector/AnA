from dotenv import load_dotenv

# Must run before any route/module reads env vars (e.g. build_session()'s
# EnvBuilder / ANA_ALLOWED_ORIGINS): without this, a developer's local .env
# (per the plan's dev-verification instructions: `cp .env.example .env` then
# run uvicorn directly, not via docker-compose) is silently ignored — only
# docker-compose's `env_file:` path actually works. No-op if no .env file is
# present (e.g. inside Docker), and does not override already-set env vars.
load_dotenv()

from fastapi import FastAPI

from apps.server.app.websocket import router as chat_router

app = FastAPI(title="ANA Core Loop")
app.include_router(chat_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
