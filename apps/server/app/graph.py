from typing import Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from packages.memory.rag import search_similar_turns, store_turn


class GraphState(TypedDict):
    prompt: str
    session_id: Optional[str]
    response_text: str
    total_cost_usd: float


# Bound on each matched turn's prompt/response before interpolation into the
# augmented prompt. turns.response is an unbounded TEXT column holding a full
# agent reply (this project already needed a 10MiB stdout reader limit for
# real long lines -- see ClaudeCodeAdapter -- so a large stored response is a
# real, not hypothetical, occurrence). The augmented prompt is passed to the
# `claude` CLI as a single argv element, and Linux caps a single argv string
# at MAX_ARG_STRLEN (131072 bytes); truncating every match's fields keeps
# even multiple matches nowhere near that limit.
_MAX_MATCH_FIELD_CHARS = 300


def _truncate(text: str, limit: int = _MAX_MATCH_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _build_augmented_prompt(prompt: str, matches: list[dict]) -> str:
    if not matches:
        return prompt
    context_lines = "\n".join(
        f"{i + 1}. Q: {_truncate(m['prompt'])}\n   A: {_truncate(m['response'])}"
        for i, m in enumerate(matches)
    )
    return (
        f"관련 과거 대화:\n{context_lines}\n\n현재 질문: {prompt}"
    )


async def call_lead_agent(state: GraphState, config: RunnableConfig) -> dict:
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
    total_cost_usd = state.get("total_cost_usd", 0.0)
    cost_usd = None
    for_error_text = ""
    caught_exc: Optional[BaseException] = None

    try:
        async for event in adapter.run_stream(augmented_prompt, session_id=session_id):
            # "text" chunks are content deltas the frontend appends as-is (see
            # useChatSocket.ts) and never inspects session_id/is_error for — so
            # only "result"/"error"/"usage" events, which carry turn-level
            # metadata, get the full payload.
            if event.type == "text":
                writer({"type": event.type, "text": event.text})
            else:
                writer(
                    {
                        "type": event.type,
                        "text": event.text,
                        "session_id": event.session_id,
                        "is_error": event.is_error,
                    }
                )
            if event.type == "result" and event.session_id:
                session_id = event.session_id
            if event.type == "text":
                response_text += event.text
            if event.type == "result":
                cost_usd = event.cost_usd
            if event.type == "error":
                for_error_text = event.text
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure
        # mid-stream (a raised exception from adapter.run_stream, as opposed
        # to a well-formed type="error" AdapterEvent) must still reach the
        # transcript/turns table before propagating, exactly like a turn
        # that ends in a type="error" event does below. Caught here rather
        # than left to an outer try/finally so the fallback response text
        # can be computed and logged, then the original exception re-raised
        # unchanged for the websocket layer to turn into an error frame.
        caught_exc = exc
    finally:
        final_response = response_text or for_error_text or (
            f"[error: {caught_exc}]" if caught_exc is not None else ""
        )
        # asyncio.CancelledError is a BaseException (not Exception, since
        # Python 3.8), so it is NOT caught by `except Exception` above — it
        # passes straight through to this `finally` block instead, exactly
        # like every normal client-disconnect teardown does (websocket.py's
        # `relay_task.cancel()`). Without this guard, a turn cancelled
        # before it produced any output (caught_exc is None and no text/
        # error text accumulated yet) would still write an empty-response
        # row to the transcript/turns table, needlessly bloating the corpus
        # with content-free rows. Only skip logging in that specific
        # nothing-happened case; an exception (caught_exc set) or an
        # error/partial-text turn still logs as before.
        if caught_exc is not None or final_response:
            transcript.append(
                session_id=session_id or "", prompt=state["prompt"], response=final_response, cost_usd=cost_usd
            )
            await store_turn(
                pool, embedding_model,
                thread_id=thread_id, session_id=session_id,
                prompt=state["prompt"], response=final_response, cost_usd=cost_usd,
            )

    if caught_exc is not None:
        raise caught_exc

    if cost_usd is not None:
        total_cost_usd += cost_usd

    return {
        "session_id": session_id,
        "response_text": final_response,
        "total_cost_usd": total_cost_usd,
    }


def build_graph(checkpointer) -> CompiledStateGraph:
    graph = StateGraph(GraphState)
    graph.add_node("call_lead_agent", call_lead_agent)
    graph.add_edge(START, "call_lead_agent")
    graph.add_edge("call_lead_agent", END)
    return graph.compile(checkpointer=checkpointer)
