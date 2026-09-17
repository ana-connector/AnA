# Memory & State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ANA persistent memory. Replace Core Loop's in-memory-only `ChatSession` with a LangGraph `StateGraph` checkpointed to PostgreSQL, so a conversation survives a server restart or a dropped WebSocket, and every turn is embedded and searchable so the lead agent can pull relevant past turns into its context.

**Architecture:** A single-node LangGraph graph (`START -> call_lead_agent -> END`) wraps the existing `ClaudeCodeAdapter` from Core Loop. `AsyncPostgresSaver` checkpoints graph state (our own bookkeeping — `session_id`, accumulated cost — not the model's own context, which the `claude` CLI still manages itself via `--resume`) keyed by a `thread_id` the browser persists in `localStorage` and resends on reconnect. Before each turn, the node searches a `turns` table (pgvector, cosine similarity over `fastembed`-generated embeddings) for the most similar past turns and prepends them to the prompt; after the turn, it stores the new turn's embedding. Streaming to the browser continues to work exactly as in Core Loop, now carried over LangGraph's `stream_mode="custom"` instead of a raw async generator.

**Tech Stack:** `langgraph` 1.2.x, `langgraph-checkpoint-postgres` 3.1.x, PostgreSQL 16 + `pgvector` extension (`pgvector/pgvector:pg16` image), `psycopg[binary,pool]` 3.3.x (async), `pgvector` (Python package) 0.5.x, `fastembed` 0.8.x (ONNX embeddings, `BAAI/bge-small-en-v1.5`, 384 dimensions — **not** `sentence-transformers`, which fails to install on this machine's disk quota; see spec).

**Spec:** `docs/plans/2026-09-17-ai-orchestrator-roadmap.md` (section "2. Memory & State")

## Global Constraints

- LangGraph is adopted now, fully replacing `ChatSession` (`apps/server/app/session_manager.py` is deleted, along with its test file).
- Embeddings: `fastembed`, model `BAAI/bge-small-en-v1.5`, 384 dimensions. Never `sentence-transformers`/PyTorch on this machine.
- RAG corpus = raw turn `prompt`+`response`, no summarization step.
- Redis is explicitly **not** introduced in this phase (deferred to Sandbox & Governance).
- A conversation must survive both a WebSocket reconnect and a server restart: the browser persists `thread_id` in `localStorage` and resends it on reconnect (as a `?thread_id=` query param); the server looks it up via the Postgres checkpointer.
- Every empirically-verified fact below was confirmed by running real code against a real `pgvector/pgvector:pg16` container and real `fastembed`/`langgraph` during planning — do not second-guess the SQL or API shapes without re-verifying.
- All new Python dependencies get pinned versions in `apps/server/requirements.txt`: `langgraph==1.2.11`, `langgraph-checkpoint-postgres==3.1.2`, `psycopg[binary,pool]==3.3.5`, `pgvector==0.5.0`, `fastembed==0.8.0`.
- Tests that need a real Postgres spin up a throwaway `pgvector/pgvector:pg16` Docker container via a shared `conftest.py` fixture (session-scoped) — Docker is already a hard dependency of this project's deployment model, so this is not a new environment requirement.
- No secret values are ever written into source files — `DATABASE_URL`'s password lives only in `.env`/`.env.example` as a placeholder, same convention as `ANTHROPIC_API_KEY_SAVED`.

---

## Empirically Verified Facts (read before implementing Tasks 1, 3, 4)

Verified during planning by running real code against a real `pgvector/pgvector:pg16` container (port 55432) and installed `langgraph`/`fastembed`:

1. **`pip install sentence-transformers` fails on this machine**: `OSError: Disk quota exceeded` (the user's home directory is near a hard per-user quota, independent of actual free disk space — do not attempt to install `torch` or anything that pulls it in). `fastembed` (ONNX, no `torch`) installs cleanly; the whole venv stayed at 328MB after adding it plus the full LangGraph/Postgres stack.
2. **`fastembed.TextEmbedding(model_name="BAAI/bge-small-en-v1.5")`** downloads a small quantized ONNX model on first use (network required once, then cached) and `.embed([text])` returns a generator of `numpy.ndarray`, shape `(384,)`, dtype `float32`. Convert with `.tolist()` to get a plain `list[float]` for storage/query.
3. **pgvector + psycopg3, INSERT vs. query parameter are NOT symmetric.** `register_vector(conn)` (sync) / `register_vector_async(conn)` (async) lets you pass a plain Python `list[float]` directly as an `INSERT ... VALUES (%s)` parameter and it round-trips correctly. But using that same plain list as a query parameter in a similarity expression (`embedding <=> %s`) fails with `psycopg.errors.UndefinedFunction: operator does not exist: vector <=> double precision[]` — Postgres can't infer the parameter is a `vector` from a bare array literal in that position. **Fix: cast explicitly in SQL — `embedding <=> %s::vector`.** This applies to `ORDER BY ... <=>` similarity searches specifically, not to inserts.
4. **`psycopg_pool.AsyncConnectionPool`** takes a `configure: AsyncConnectionCB` callback invoked once per newly-created connection (not per-checkout) — this is the right place to call `await register_vector_async(conn)` so every pooled connection can adapt `vector` values, matching fact #3 above.
5. **LangGraph node functions can take `(state, config)`.** `config["configurable"]` contains exactly what the caller passed in `compiled.astream(..., config={"configurable": {...}})`, plus LangGraph's own internal `__pregel_*` keys — custom keys (e.g. `adapter`, `pool`, `embedding_model`, `transcript`) pass through untouched and are the correct way to inject per-call dependencies (including test doubles) without patching module globals.
6. **Custom streaming**: inside a node, `from langgraph.config import get_stream_writer; writer = get_stream_writer(); writer({...})` pushes an arbitrary dict; the caller receives it by iterating `compiled.astream(input, config=..., stream_mode="custom")`, yielding exactly the dicts passed to `writer(...)`, in order, interleaved with the node's execution.
7. **Checkpointing survives a fresh `AsyncPostgresSaver` instance** (simulating a server restart): `await compiled.aget_state(config)` after re-opening a new `AsyncPostgresSaver.from_conn_string(dsn)` against the same `thread_id` returns the exact state dict left by the previous instance. `AsyncPostgresSaver.from_conn_string(conn_string)` is an **async context manager** (`async with ... as checkpointer:`); call `await checkpointer.setup()` once to create/migrate its own tables (idempotent, safe to call every startup).

---

## File Structure

```
ana/
├── apps/
│   ├── server/
│   │   ├── requirements.txt              # modified — add 5 new pinned deps
│   │   ├── app/
│   │   │   ├── main.py                   # modified — lifespan: pool/checkpointer/graph setup
│   │   │   ├── graph.py                  # new — LangGraph StateGraph, call_lead_agent node
│   │   │   ├── websocket.py              # modified — graph-based turn processing, thread_id
│   │   │   └── session_manager.py        # DELETED (replaced by graph.py)
│   │   └── tests/
│   │       ├── conftest.py               # new — postgres_dsn, db_pool fixtures
│   │       ├── test_memory_db.py         # new
│   │       ├── test_memory_embeddings.py # new
│   │       ├── test_memory_rag.py        # new
│   │       ├── test_graph.py             # new
│   │       ├── test_main.py              # modified — lifespan-aware health check
│   │       ├── test_websocket.py         # modified — graph-based fakes instead of ChatSession
│   │       └── test_session_manager.py   # DELETED
│   └── web/
│       └── src/hooks/useChatSocket.ts    # modified — thread_id persistence
├── packages/
│   └── memory/
│       ├── __init__.py                   # new
│       ├── db.py                         # new — pool + migrations
│       ├── embeddings.py                 # new — EmbeddingModel
│       └── rag.py                        # new — store_turn, search_similar_turns
├── docker-compose.yml                    # modified — add postgres service
└── .env.example                          # modified — add DATABASE_URL
```

---

### Task 1: Postgres connection pool, schema migration, shared test fixture

**Files:**
- Create: `packages/memory/__init__.py`, `packages/memory/db.py`
- Create: `apps/server/tests/conftest.py`
- Test: `apps/server/tests/test_memory_db.py`
- Modify: `apps/server/requirements.txt`

**Interfaces:**
- Produces: `create_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool`, `run_migrations(pool: AsyncConnectionPool) -> None`; pytest fixtures `postgres_dsn` (session-scoped) and `db_pool` (function-scoped, truncates `turns` before each test)

- [ ] **Step 1: Add new dependencies**

Append to `apps/server/requirements.txt`:
```
langgraph==1.2.11
langgraph-checkpoint-postgres==3.1.2
psycopg[binary,pool]==3.3.5
pgvector==0.5.0
fastembed==0.8.0
```

```bash
cd /home/hello/ana
.venv/bin/pip install -r apps/server/requirements.txt
```

- [ ] **Step 2: Write the shared Postgres test fixture**

`apps/server/tests/conftest.py`:
```python
import socket
import subprocess
import time
import uuid

import pytest
import pytest_asyncio


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_postgres(container_name: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", container_name, "pg_isready", "-U", "postgres"],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"postgres in {container_name} did not become ready in {timeout}s")


@pytest.fixture(scope="session")
def postgres_dsn():
    """Spins up a throwaway pgvector-enabled Postgres in Docker for the
    whole test session. Requires a working Docker daemon (already a hard
    dependency of this project's deployment model — see docker-compose.yml)."""
    container_name = f"ana-test-pg-{uuid.uuid4().hex[:8]}"
    port = _find_free_port()
    subprocess.run(
        [
            "docker", "run", "-d", "--name", container_name,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=ana_test",
            "-p", f"{port}:5432",
            "pgvector/pgvector:pg16",
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres:test@localhost:{port}/ana_test"
    try:
        _wait_for_postgres(container_name)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


@pytest_asyncio.fixture
async def db_pool(postgres_dsn):
    from packages.memory.db import create_pool, run_migrations

    pool = await create_pool(postgres_dsn)
    await run_migrations(pool)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE turns")
    yield pool
    await pool.close()
```

- [ ] **Step 3: Write the failing test**

`apps/server/tests/test_memory_db.py`:
```python
import pytest


@pytest.mark.asyncio
async def test_run_migrations_creates_turns_table(db_pool):
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'turns' ORDER BY column_name"
        )
        columns = {row[0] for row in await cur.fetchall()}
    assert {"id", "thread_id", "session_id", "prompt", "response", "cost_usd", "embedding", "created_at"} <= columns


@pytest.mark.asyncio
async def test_run_migrations_is_idempotent(db_pool):
    from packages.memory.db import run_migrations

    await run_migrations(db_pool)  # second call must not raise
    await run_migrations(db_pool)


@pytest.mark.asyncio
async def test_pool_connection_can_insert_and_read_a_vector(db_pool):
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO turns (thread_id, session_id, prompt, response, cost_usd, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ("t1", "s1", "hi", "hello", 0.01, [0.1] * 384),
        )
        cur = await conn.execute("SELECT prompt, response FROM turns WHERE thread_id = %s", ("t1",))
        row = await cur.fetchone()
    assert row == ("hi", "hello")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_memory_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.memory.db'`

- [ ] **Step 5: Write minimal implementation**

`packages/memory/__init__.py`: (empty)

`packages/memory/db.py`:
```python
from psycopg_pool import AsyncConnectionPool
from pgvector.psycopg import register_vector_async

EMBEDDING_DIMENSIONS = 384

MIGRATIONS = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS turns (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    session_id TEXT,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    cost_usd DOUBLE PRECISION,
    embedding VECTOR({EMBEDDING_DIMENSIONS}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turns_embedding_idx ON turns
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
"""


async def create_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    async def configure(conn):
        await register_vector_async(conn)

    pool = AsyncConnectionPool(
        dsn, min_size=min_size, max_size=max_size, configure=configure, open=False
    )
    await pool.open(wait=True)
    return pool


async def run_migrations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(MIGRATIONS)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_memory_db.py -v`
Expected: PASS (3 tests). First run downloads the `pgvector/pgvector:pg16` Docker image if not cached — allow extra time.

- [ ] **Step 7: Commit**

```bash
git add apps/server/requirements.txt apps/server/tests/conftest.py \
  packages/memory/__init__.py packages/memory/db.py apps/server/tests/test_memory_db.py
git commit -m "feat: add Postgres pool, pgvector schema migration, shared test fixture"
```

---

### Task 2: Embedding model wrapper

**Files:**
- Create: `packages/memory/embeddings.py`
- Test: `apps/server/tests/test_memory_embeddings.py`

**Interfaces:**
- Produces: `EmbeddingModel(model_name: str = "BAAI/bge-small-en-v1.5")` with `.embed_one(text: str) -> list[float]` and `.embed_many(texts: list[str]) -> list[list[float]]`; class attribute `DIMENSIONS = 384`

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/test_memory_embeddings.py`:
```python
from packages.memory.embeddings import EmbeddingModel


def test_embed_one_returns_384_floats():
    model = EmbeddingModel()
    vector = model.embed_one("hello world")
    assert len(vector) == 384
    assert all(isinstance(x, float) for x in vector)


def test_embed_many_matches_embed_one_per_item():
    model = EmbeddingModel()
    vectors = model.embed_many(["hello world", "goodbye world"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 384
    assert len(vectors[1]) == 384


def test_identical_text_yields_identical_embedding():
    model = EmbeddingModel()
    assert model.embed_one("same text") == model.embed_one("same text")


def test_different_text_yields_different_embedding():
    model = EmbeddingModel()
    assert model.embed_one("apple") != model.embed_one("astrophysics")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_memory_embeddings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.memory.embeddings'`

- [ ] **Step 3: Write minimal implementation**

`packages/memory/embeddings.py`:
```python
from fastembed import TextEmbedding


class EmbeddingModel:
    """Wraps fastembed (ONNX-based, no PyTorch). sentence-transformers was
    rejected — it fails to install on this machine's disk quota (see the
    roadmap doc, '임베딩 모델')."""

    DIMENSIONS = 384

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self._model = TextEmbedding(model_name=model_name)

    def embed_one(self, text: str) -> list[float]:
        return next(self._model.embed([text])).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_memory_embeddings.py -v`
Expected: PASS (4 tests). First run downloads the ONNX model file (network required once, then cached).

- [ ] **Step 5: Commit**

```bash
git add packages/memory/embeddings.py apps/server/tests/test_memory_embeddings.py
git commit -m "feat: add fastembed-based EmbeddingModel wrapper"
```

---

### Task 3: RAG store and search

**Files:**
- Create: `packages/memory/rag.py`
- Test: `apps/server/tests/test_memory_rag.py`

**Interfaces:**
- Consumes: `create_pool`/`run_migrations` (`packages.memory.db`), `EmbeddingModel` (`packages.memory.embeddings`)
- Produces: `async store_turn(pool, embedding_model, *, thread_id, session_id, prompt, response, cost_usd) -> None`, `async search_similar_turns(pool, embedding_model, query, *, limit=3) -> list[dict]` (each dict: `prompt`, `response`, `distance`)

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/test_memory_rag.py`:
```python
import pytest

from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import search_similar_turns, store_turn


@pytest.fixture(scope="module")
def embedding_model():
    return EmbeddingModel()


@pytest.mark.asyncio
async def test_store_then_search_finds_the_most_similar_turn(db_pool, embedding_model):
    await store_turn(
        db_pool, embedding_model,
        thread_id="t1", session_id="s1",
        prompt="What's the capital of France?", response="Paris.",
        cost_usd=0.01,
    )
    await store_turn(
        db_pool, embedding_model,
        thread_id="t1", session_id="s1",
        prompt="Write a haiku about the ocean", response="Waves crash on the shore...",
        cost_usd=0.02,
    )

    results = await search_similar_turns(db_pool, embedding_model, "What city is France's capital?", limit=1)

    assert len(results) == 1
    assert results[0]["prompt"] == "What's the capital of France?"
    assert results[0]["response"] == "Paris."
    assert 0.0 <= results[0]["distance"] < 1.0


@pytest.mark.asyncio
async def test_search_on_empty_table_returns_empty_list(db_pool, embedding_model):
    results = await search_similar_turns(db_pool, embedding_model, "anything", limit=3)
    assert results == []


@pytest.mark.asyncio
async def test_search_respects_limit(db_pool, embedding_model):
    for i in range(5):
        await store_turn(
            db_pool, embedding_model,
            thread_id="t1", session_id="s1",
            prompt=f"question number {i}", response=f"answer number {i}",
            cost_usd=None,
        )

    results = await search_similar_turns(db_pool, embedding_model, "question", limit=2)
    assert len(results) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_memory_rag.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.memory.rag'`

- [ ] **Step 3: Write minimal implementation**

`packages/memory/rag.py`:
```python
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from packages.memory.embeddings import EmbeddingModel


async def store_turn(
    pool: AsyncConnectionPool,
    embedding_model: EmbeddingModel,
    *,
    thread_id: str,
    session_id: Optional[str],
    prompt: str,
    response: str,
    cost_usd: Optional[float],
) -> None:
    embedding = embedding_model.embed_one(f"{prompt}\n{response}")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO turns (thread_id, session_id, prompt, response, cost_usd, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (thread_id, session_id, prompt, response, cost_usd, embedding),
        )


async def search_similar_turns(
    pool: AsyncConnectionPool,
    embedding_model: EmbeddingModel,
    query: str,
    *,
    limit: int = 3,
) -> list[dict]:
    embedding = embedding_model.embed_one(query)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT prompt, response, embedding <=> %s::vector AS distance
            FROM turns
            ORDER BY distance
            LIMIT %s
            """,
            (embedding, limit),
        )
        rows = await cur.fetchall()
    return [{"prompt": r[0], "response": r[1], "distance": r[2]} for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_memory_rag.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add packages/memory/rag.py apps/server/tests/test_memory_rag.py
git commit -m "feat: add RAG turn storage and pgvector similarity search"
```

---

### Task 4: LangGraph graph wrapping the lead agent

**Files:**
- Create: `apps/server/app/graph.py`
- Test: `apps/server/tests/test_graph.py`

**Interfaces:**
- Consumes: `AgentAdapter`/`AdapterEvent` (`packages.adapters.base`), `TranscriptLogger` (`packages.adapters.transcript`), `search_similar_turns`/`store_turn` (`packages.memory.rag`), `EmbeddingModel` (`packages.memory.embeddings`)
- Produces: `GraphState` (TypedDict), `build_graph(checkpointer) -> CompiledStateGraph`. Per-call dependencies (`adapter`, `pool`, `embedding_model`, `transcript`) are injected via `config["configurable"]` (see Empirically Verified Facts #5) — this is what makes the node testable with fakes without patching module globals.

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/test_graph.py`:
```python
from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import build_graph
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import search_similar_turns, store_turn


class FakeAdapter(AgentAdapter):
    def __init__(self, prompts_seen: list[str]):
        self.prompts_seen = prompts_seen

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.prompts_seen.append(prompt)
        yield AdapterEvent(type="text", text="hi there", session_id="cli-sid-1")
        yield AdapterEvent(type="result", text="hi there", session_id="cli-sid-1", cost_usd=0.01)

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_graph_streams_events_and_persists_state(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    prompts_seen: list[str] = []
    adapter = FakeAdapter(prompts_seen)
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "adapter": adapter,
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": Mock(),
        }
    }

    events = [
        chunk
        async for chunk in graph.astream(
            {"prompt": "hello", "session_id": None, "response_text": ""},
            config=config,
            stream_mode="custom",
        )
    ]

    assert {"type": "text", "text": "hi there"} in events
    assert any(e["type"] == "result" for e in events)

    state = await graph.aget_state(config)
    assert state.values["session_id"] == "cli-sid-1"

    stored = await search_similar_turns(db_pool, embedding_model, "hello", limit=1)
    assert stored[0]["prompt"] == "hello"
    assert stored[0]["response"] == "hi there"


@pytest.mark.asyncio
async def test_graph_prepends_similar_past_turns_to_the_prompt(db_pool):
    embedding_model = EmbeddingModel()
    await store_turn(
        db_pool, embedding_model,
        thread_id="unrelated", session_id=None,
        prompt="What's the capital of France?", response="Paris.",
        cost_usd=None,
    )

    graph = build_graph(InMemorySaver())
    prompts_seen: list[str] = []
    adapter = FakeAdapter(prompts_seen)
    config = {
        "configurable": {
            "thread_id": "thread-2",
            "adapter": adapter,
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": Mock(),
        }
    }

    async for _ in graph.astream(
        {"prompt": "What city is France's capital?", "session_id": None, "response_text": ""},
        config=config,
        stream_mode="custom",
    ):
        pass

    assert len(prompts_seen) == 1
    assert "Paris" in prompts_seen[0]
    assert "What city is France's capital?" in prompts_seen[0]


@pytest.mark.asyncio
async def test_graph_state_survives_a_fresh_checkpointer_instance(db_pool):
    """Simulates a server restart: a brand new InMemorySaver would NOT
    survive this (it's process-local), so this test uses two separate
    build_graph() calls sharing the SAME checkpointer instance to prove
    thread-scoped state round-trips through it — the real AsyncPostgresSaver
    variant of this guarantee is covered by Task 5's lifespan test against
    real Postgres."""
    from langgraph.checkpoint.memory import InMemorySaver

    shared_checkpointer = InMemorySaver()
    embedding_model = EmbeddingModel()
    config = {"configurable": {"thread_id": "thread-3"}}

    graph_a = build_graph(shared_checkpointer)
    async for _ in graph_a.astream(
        {"prompt": "hi", "session_id": None, "response_text": ""},
        config={
            **config,
            "configurable": {
                **config["configurable"],
                "adapter": FakeAdapter([]),
                "pool": db_pool,
                "embedding_model": embedding_model,
                "transcript": Mock(),
            },
        },
        stream_mode="custom",
    ):
        pass

    graph_b = build_graph(shared_checkpointer)
    state = await graph_b.aget_state(config)
    assert state.values["session_id"] == "cli-sid-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_graph.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.server.app.graph'`

- [ ] **Step 3: Write minimal implementation**

`apps/server/app/graph.py`:
```python
from typing import Optional, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from packages.memory.rag import search_similar_turns, store_turn


class GraphState(TypedDict):
    prompt: str
    session_id: Optional[str]
    response_text: str


def _build_augmented_prompt(prompt: str, matches: list[dict]) -> str:
    if not matches:
        return prompt
    context_lines = "\n".join(
        f"{i + 1}. Q: {m['prompt']}\n   A: {m['response']}" for i, m in enumerate(matches)
    )
    return (
        f"관련 과거 대화:\n{context_lines}\n\n현재 질문: {prompt}"
    )


async def call_lead_agent(state: GraphState, config: dict) -> dict:
    configurable = config["configurable"]
    adapter = configurable["adapter"]
    pool = configurable["pool"]
    embedding_model = configurable["embedding_model"]
    transcript = configurable["transcript"]
    thread_id = configurable["thread_id"]

    writer = get_stream_writer()

    matches = await search_similar_turns(pool, embedding_model, state["prompt"], limit=3)
    augmented_prompt = _build_augmented_prompt(state["prompt"], matches)

    response_text = ""
    session_id = state.get("session_id")
    cost_usd = None
    for_error_text = ""

    async for event in adapter.run_stream(augmented_prompt, session_id=session_id):
        writer({"type": event.type, "text": event.text, "session_id": event.session_id, "is_error": event.is_error})
        if event.type == "result" and event.session_id:
            session_id = event.session_id
        if event.type == "text":
            response_text += event.text
        if event.type == "result":
            cost_usd = event.cost_usd
        if event.type == "error":
            for_error_text = event.text

    final_response = response_text or for_error_text
    transcript.append(session_id=session_id or "", prompt=state["prompt"], response=final_response, cost_usd=cost_usd)
    await store_turn(
        pool, embedding_model,
        thread_id=thread_id, session_id=session_id,
        prompt=state["prompt"], response=final_response, cost_usd=cost_usd,
    )

    return {"session_id": session_id, "response_text": final_response}


def build_graph(checkpointer) -> CompiledStateGraph:
    graph = StateGraph(GraphState)
    graph.add_node("call_lead_agent", call_lead_agent)
    graph.add_edge(START, "call_lead_agent")
    graph.add_edge("call_lead_agent", END)
    return graph.compile(checkpointer=checkpointer)
```

**Note on the error path:** `session_id` is only adopted from a `type=="result"` event (matching Core Loop's I2 fix — never from an `error` event), so a failed turn never poisons the thread's persisted `session_id`. `transcript.append`/`store_turn` still run for an error turn (matching Core Loop's I1 fix), using the error's own text as the response when nothing was streamed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_graph.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/app/graph.py apps/server/tests/test_graph.py
git commit -m "feat: add LangGraph single-node graph wrapping ClaudeCodeAdapter with RAG augmentation"
```

---

### Task 5: FastAPI lifespan — Postgres pool, checkpointer, graph on `app.state`

**Files:**
- Modify: `apps/server/app/main.py`
- Test: `apps/server/tests/test_main.py`

**Interfaces:**
- Produces: `app.state.pool: AsyncConnectionPool`, `app.state.embedding_model: EmbeddingModel`, `app.state.checkpointer: AsyncPostgresSaver`, `app.state.graph: CompiledStateGraph`, all created on startup and torn down on shutdown via a FastAPI `lifespan` context manager.

- [ ] **Step 1: Write the failing test**

`apps/server/tests/test_main.py` (full replacement — this grows beyond the Core Loop health-check-only version):
```python
import contextlib

from fastapi.testclient import TestClient

from apps.server.app.main import app


def test_healthz_returns_ok(monkeypatch, postgres_dsn):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lifespan_attaches_pool_checkpointer_and_graph(monkeypatch, postgres_dsn):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as client:
        assert client.app.state.pool is not None
        assert client.app.state.checkpointer is not None
        assert client.app.state.graph is not None
        assert client.app.state.embedding_model is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest apps/server/tests/test_main.py -v`
Expected: FAIL — `client.app.state.pool` raises `AttributeError` (no lifespan wiring yet)

- [ ] **Step 3: Write minimal implementation**

`apps/server/app/main.py`:
```python
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest apps/server/tests/test_main.py -v`
Expected: PASS (2 tests). Note `EmbeddingModel()` instantiation on every app startup downloads/loads the ONNX model — acceptable at this scale (single process, not per-request), but slow on the very first run.

- [ ] **Step 5: Commit**

```bash
git add apps/server/app/main.py apps/server/tests/test_main.py
git commit -m "feat: wire Postgres pool, checkpointer, and graph into FastAPI lifespan"
```

---

### Task 6: Rewrite the WebSocket endpoint around the graph; remove `ChatSession`

**Files:**
- Modify: `apps/server/app/websocket.py`
- Delete: `apps/server/app/session_manager.py`, `apps/server/tests/test_session_manager.py`
- Test: `apps/server/tests/test_websocket.py` (full replacement)

**Interfaces:**
- Consumes: `app.state.graph`/`pool`/`embedding_model` (Task 5), `ClaudeCodeAdapter` (`packages.adapters.claude_code`), `EnvBuilder`/settings functions (`packages.adapters.env_builder`), `TranscriptLogger` (`packages.adapters.transcript`)
- Produces: `WS /ws/chat?thread_id=<uuid>` (query param optional — server generates one if absent); first server message is always `{"type": "thread_id", "text": "<uuid>", "session_id": null, "is_error": false}` so the browser can persist it.

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/test_websocket.py` (full replacement of the Core Loop version — `ChatSession`/`build_session` are gone, replaced by the graph):
```python
from typing import AsyncIterator, Optional
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.server.app.main import app
from packages.adapters.base import AdapterEvent, AgentAdapter


class FakeAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text=f"echo:{prompt}", session_id="sid-1")
        yield AdapterEvent(type="result", text=f"echo:{prompt}", session_id="sid-1", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


class RaisingAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        raise RuntimeError("boom: adapter exploded")
        yield  # pragma: no cover

    async def terminate(self) -> None:
        return None


@pytest.fixture
def client_with_fake_adapter(postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=FakeAdapter()
    ):
        with TestClient(app) as client:
            yield client


def test_websocket_sends_thread_id_first_then_echoes_prompt(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect("/ws/chat") as ws:
        first = ws.receive_json()
        assert first["type"] == "thread_id"
        assert len(first["text"]) > 0

        ws.send_text("hello")
        second = ws.receive_json()
        third = ws.receive_json()

    assert second == {"type": "text", "text": "echo:hello", "session_id": "sid-1", "is_error": False}
    assert third["type"] == "result"


def test_websocket_reuses_supplied_thread_id(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect("/ws/chat?thread_id=fixed-thread-abc") as ws:
        first = ws.receive_json()
    assert first == {"type": "thread_id", "text": "fixed-thread-abc", "session_id": None, "is_error": False}


def test_websocket_rejects_disallowed_origin(postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    mock_build = Mock(side_effect=AssertionError("build_adapter should not be called"))
    with patch("apps.server.app.websocket.build_adapter", mock_build):
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(
                    "/ws/chat", headers={"origin": "http://evil.example.com"}
                ) as ws:
                    ws.receive_text()
    assert exc_info.value.code == 1008
    mock_build.assert_not_called()


def test_websocket_relays_error_when_run_stream_raises(postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch("apps.server.app.websocket.build_adapter", return_value=RaisingAdapter()):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id
                ws.send_text("hello")
                event = ws.receive_json()
    assert event["type"] == "error"
    assert "boom" in event["text"]
```

- [ ] **Step 2: Delete the superseded files**

```bash
git rm apps/server/app/session_manager.py apps/server/tests/test_session_manager.py
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_websocket.py -v`
Expected: FAIL — `apps.server.app.websocket.build_adapter` doesn't exist yet (`AttributeError` via `mock.patch`'s target resolution, same benign shape as Core Loop's Task 7)

- [ ] **Step 4: Write minimal implementation**

`apps/server/app/websocket.py`:
```python
import asyncio
import contextlib
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from packages.adapters.claude_code import ClaudeCodeAdapter
from packages.adapters.env_builder import (
    EnvBuilder,
    default_effort_from_settings,
    default_mode_from_settings,
    default_model_from_settings,
)
from packages.adapters.transcript import TranscriptLogger

router = APIRouter()

WORKSPACE_DIR = os.environ.get("ANA_WORKSPACE_DIR", "/home/hello/workspace")


def _allowed_origins() -> set[str]:
    raw = os.environ.get("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    return {origin.strip() for origin in raw.split(",") if origin.strip()}


def build_adapter():
    mode = default_mode_from_settings()
    env = EnvBuilder(mode).build()
    return ClaudeCodeAdapter(
        workspace_dir=WORKSPACE_DIR,
        env=env,
        model=default_model_from_settings(),
        effort=default_effort_from_settings(),
    )


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    origin = websocket.headers.get("origin")
    if origin is not None and origin not in _allowed_origins():
        await websocket.close(code=1008)
        return

    thread_id = websocket.query_params.get("thread_id") or str(uuid.uuid4())
    await websocket.send_json(
        {"type": "thread_id", "text": thread_id, "session_id": None, "is_error": False}
    )

    adapter = build_adapter()
    transcript = TranscriptLogger(WORKSPACE_DIR)
    graph = websocket.app.state.graph
    config = {
        "configurable": {
            "thread_id": thread_id,
            "adapter": adapter,
            "pool": websocket.app.state.pool,
            "embedding_model": websocket.app.state.embedding_model,
            "transcript": transcript,
        }
    }

    queue: "asyncio.Queue[str]" = asyncio.Queue()
    relay_failed = asyncio.Event()

    async def relay_events() -> None:
        try:
            while True:
                prompt = await queue.get()
                snapshot = await graph.aget_state(config)
                session_id = (snapshot.values or {}).get("session_id")
                async for event in graph.astream(
                    {"prompt": prompt, "session_id": session_id, "response_text": ""},
                    config=config,
                    stream_mode="custom",
                ):
                    await websocket.send_json(event)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "text": str(exc), "session_id": None, "is_error": True}
                )
            relay_failed.set()

    relay_task = asyncio.create_task(relay_events())
    try:
        while True:
            receive_task = asyncio.ensure_future(websocket.receive_text())
            failure_task = asyncio.ensure_future(relay_failed.wait())
            done, pending = await asyncio.wait(
                {receive_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if failure_task in done:
                with contextlib.suppress(Exception):
                    await websocket.close(code=1011)
                break
            data = await receive_task
            await queue.put(data)
    except WebSocketDisconnect:
        pass
    finally:
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay_task
        await adapter.terminate()
```

**Design notes carried forward from Core Loop's final review (do not regress these):**
- `build_session()`'s failure-before-`accept()` path doesn't apply the same way here (`build_adapter()` is trivial and can't raise the way the old `EnvBuilder`-in-api-mode-with-no-key case could — that failure now surfaces inside `relay_events()`'s first turn instead, which is already handled by the try/except there).
- The **queue lives in the websocket handler now, not inside a `ChatSession` object** — `graph.astream()` is called once per turn (not once forever), so the queueing that `ChatSession.run()` used to provide internally is now explicit here: `relay_events()` loops pulling one prompt at a time off `queue`, matching Core Loop's "동시 메시지 처리: 큐잉" constraint.
- Session id continuity across turns now comes from `graph.aget_state(config)` (Postgres-backed) instead of an in-memory attribute — this is the whole point of this subproject.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests -v`
Expected: PASS — full suite, no leftover references to `ChatSession`/`session_manager`.

- [ ] **Step 6: Commit**

```bash
git add apps/server/app/websocket.py apps/server/tests/test_websocket.py
git rm apps/server/app/session_manager.py apps/server/tests/test_session_manager.py
git commit -m "feat: rewrite /ws/chat around the LangGraph graph, remove ChatSession"
```

---

### Task 7: Frontend `thread_id` persistence

**Files:**
- Modify: `apps/web/src/hooks/useChatSocket.ts`

**Interfaces:**
- Produces: on connect, reads `localStorage.getItem("ana_thread_id")` and appends it as `?thread_id=` to the WebSocket URL if present; on receiving a `type: "thread_id"` event, saves it to `localStorage` (covers both the "server assigned a new one" and "server confirmed the one we sent" cases).

- [ ] **Step 1: Modify the hook**

In `apps/web/src/hooks/useChatSocket.ts`, change the connection setup and `onmessage` handler:

```typescript
useEffect(() => {
    const storedThreadId =
      typeof window !== "undefined" ? window.localStorage.getItem("ana_thread_id") : null;
    const url = storedThreadId
      ? `${WS_URL}${WS_URL.includes("?") ? "&" : "?"}thread_id=${encodeURIComponent(storedThreadId)}`
      : WS_URL;

    const socket = new WebSocket(url);
    socketRef.current = socket;

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);

    socket.onmessage = (raw) => {
      const event = JSON.parse(raw.data) as ChatEvent;

      if (event.type === "thread_id") {
        window.localStorage.setItem("ana_thread_id", event.text);
        return;
      }

      if (event.type === "text") {
        setMessages((prev) => {
          if (agentMessageIndexRef.current === null) {
            agentMessageIndexRef.current = prev.length;
            return [...prev, { role: "agent", text: event.text }];
          }
          const index = agentMessageIndexRef.current;
          const next = [...prev];
          next[index] = { role: "agent", text: next[index].text + event.text };
          return next;
        });
      }

      if (event.type === "result") {
        agentMessageIndexRef.current = null;
      }

      if (event.type === "error") {
        setMessages((prev) => [...prev, { role: "agent", text: `⚠️ ${event.text}` }]);
        agentMessageIndexRef.current = null;
      }
    };

    return () => socket.close();
  }, []);
```

Also add `"thread_id"` to the `ChatEvent["type"]` union at the top of the file.

- [ ] **Step 2: Verify the build stays clean**

```bash
cd /home/hello/ana/apps/web
npm run build
npm run lint
```

Expected: both clean (no type errors — `"thread_id"` must be a valid member of the `ChatEvent["type"]` union used elsewhere in the file).

- [ ] **Step 3: Commit**

```bash
cd /home/hello/ana
git add apps/web/src/hooks/useChatSocket.ts
git commit -m "feat: persist LangGraph thread_id in localStorage across reconnects"
```

---

### Task 8: `docker-compose.yml` and `.env.example` — add Postgres

**Files:**
- Modify: `docker-compose.yml`, `.env.example`

- [ ] **Step 1: Add the `postgres` service**

In `docker-compose.yml`, add a new service and wire `server` to depend on it and receive `DATABASE_URL`:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-ana_dev_password}
      - POSTGRES_DB=ana
    ports:
      - "5432:5432"
    volumes:
      - ana_postgres_data:/var/lib/postgresql/data

  server:
    build:
      context: .
      dockerfile: apps/server/Dockerfile
    ports:
      - "8000:8000"
    env_file:
      - .env
    environment:
      - ANA_WORKSPACE_DIR=/home/hello/workspace
      - DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD:-ana_dev_password}@postgres:5432/ana
    volumes:
      - /home/hello/workspace:/home/hello/workspace
      - ${HOME}/.claude:/root/.claude
    depends_on:
      - postgres

  web:
    build:
      context: apps/web
      args:
        NEXT_PUBLIC_WS_URL: ${NEXT_PUBLIC_WS_URL:-ws://localhost:8000/ws/chat}
    ports:
      - "3000:3000"
    depends_on:
      - server

volumes:
  ana_postgres_data:
```

(This preserves every service/setting from Core Loop's compose file — `server`'s `ANA_ALLOWED_ORIGINS` still comes from `env_file: .env`, unchanged.)

- [ ] **Step 2: Add Postgres vars to `.env.example`**

Append to `.env.example`:
```
# Postgres (pgvector). DATABASE_URL below must match POSTGRES_PASSWORD when
# running via docker-compose; for local (non-docker) dev against a Postgres
# you run yourself, set DATABASE_URL directly and ignore POSTGRES_PASSWORD.
POSTGRES_PASSWORD=ana_dev_password
DATABASE_URL=postgresql://postgres:ana_dev_password@localhost:5432/ana
```

- [ ] **Step 3: Verify the server image still builds**

```bash
cd /home/hello/ana
docker compose build server
```

Expected: succeeds (no new system packages needed — `psycopg[binary]` ships its own compiled `libpq`, no separate `apt-get install libpq-dev` required).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat: add postgres service to docker-compose"
```

---

### Task 9: End-to-end verification against the real stack

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `.venv/bin/pytest apps/server/tests -v`
Expected: PASS, all tests (Tasks 1–6's tests, still using the real Docker-Postgres fixture and the fake CLI, zero real Claude API calls).

- [ ] **Step 2: Start a real Postgres and the server against the real `claude` CLI**

```bash
cd /home/hello/ana
docker run -d --name ana-verify-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ana -p 5433:5432 pgvector/pgvector:pg16
DATABASE_URL=postgresql://postgres:test@localhost:5433/ana \
  CLAUDE_AUTH_MODE=pro \
  ANA_WORKSPACE_DIR=/home/hello/workspace \
  .venv/bin/python -m uvicorn apps.server.app.main:app --host 0.0.0.0 --port 8010 --app-dir /home/hello/ana &
```

(Use `pro` mode unless Core Loop's Task 10 already confirmed `api` mode works on this machine — see that plan's findings before assuming otherwise.)

- [ ] **Step 3: Prove state survives a WebSocket reconnect (not just a queued double-send)**

Using a small script (the pattern from Core Loop's Task 10 `ws_e2e_check.py`, extended):
1. Connect to `ws://localhost:8010/ws/chat`, capture the `thread_id` from the first frame.
2. Send `"my favorite number is 42, remember it"`, wait for the `result` event.
3. **Close the connection.**
4. Reconnect to `ws://localhost:8010/ws/chat?thread_id=<same thread_id>`.
5. Send `"what's my favorite number?"`.

Expected: the reply references 42 — proving the underlying CLI session (reached via `--resume`, whose id was persisted by the Postgres checkpointer under this `thread_id`) survived the disconnect, not just that the process stayed up.

- [ ] **Step 4: Prove RAG search surfaces a past turn across threads**

1. In a **new** thread (no `thread_id` param, fresh connection), send a prompt semantically close to the "favorite number is 42" turn from Step 3 — e.g. `"do you know anything about my favorite number?"`.
2. Confirm (via server logs, or by temporarily logging `matches` in `call_lead_agent`, or simply by the reply's content) that the augmented prompt included the prior turn, even though this is a different `thread_id`.

- [ ] **Step 5: Clean up**

```bash
pkill -f "uvicorn apps.server.app.main:app.*8010"
docker rm -f ana-verify-pg
```

Confirm `git status` is clean (no stray files from manual verification).

- [ ] **Step 6: Commit the final state (if anything changed)**

```bash
git status
# if the manual run touched any tracked files, review with git diff, then:
git add -A
git commit -m "chore: verify Memory & State end-to-end (reconnect resumes conversation, cross-thread RAG recall)"
```

This task has no code changes expected under normal circumstances — it is the acceptance check for "Memory & State" as defined in the roadmap: *a conversation survives a dropped connection, and the lead agent can recall a relevant past turn even in a brand-new thread.*
