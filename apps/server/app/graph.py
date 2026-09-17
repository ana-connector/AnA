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


def _build_augmented_prompt(prompt: str, matches: list[dict]) -> str:
    if not matches:
        return prompt
    context_lines = "\n".join(
        f"{i + 1}. Q: {m['prompt']}\n   A: {m['response']}" for i, m in enumerate(matches)
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
    cost_usd = None
    for_error_text = ""

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
