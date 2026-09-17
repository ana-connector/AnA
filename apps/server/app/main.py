import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Must run before any route/module reads env vars (e.g. build_adapter()'s
# EnvBuilder / ANA_ALLOWED_ORIGINS): without this, a developer's local .env
# (per the plan's dev-verification instructions: `cp .env.example .env` then
# run uvicorn directly, not via docker-compose) is silently ignored — only
# docker-compose's `env_file:` path actually works. No-op if no .env file is
# present (e.g. inside Docker), and does not override already-set env vars.
load_dotenv()

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from apps.server.app.graph import build_graph
from apps.server.app.websocket import router as chat_router
from packages.memory.db import create_pool, run_migrations
from packages.memory.embeddings import EmbeddingModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ana")

    pool = await create_pool(dsn)
    await run_migrations(pool)

    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()

        app.state.pool = pool
        app.state.embedding_model = EmbeddingModel()
        app.state.checkpointer = checkpointer
        app.state.graph = build_graph(checkpointer)

        yield

    await pool.close()


app = FastAPI(title="ANA Core Loop", lifespan=lifespan)
app.include_router(chat_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
