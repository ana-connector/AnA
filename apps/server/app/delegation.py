"""Run lead-requested tasks through the worker routing ladder."""
import uuid
from typing import Awaitable, Callable

from apps.server.app.graph import _run_worker
from packages.governance.secrets import mask as mask_secrets
from packages.governance.subagent_scope import Scope
from packages.observability.metrics import record_failover
from packages.orchestrator.breaker import is_recoverable_failure
from packages.orchestrator.routing import WORKER, decide, load_routing_table


async def run_routed_task(
    title: str,
    prompt: str,
    *,
    worker_factory: Callable,
    workspace_dir: str,
    usage_tracker,
    native_agents: dict,
    send: Callable[[dict], Awaitable[None]],
    secrets: list | None = None,
) -> dict:
    agent_id = f"routed:{uuid.uuid4().hex[:12]}"
    selected = decide(None, role=WORKER, usage=usage_tracker.snapshot())
    rungs = load_routing_table().rungs_for(WORKER)
    selected_index = next((i for i, rung in enumerate(rungs)
                           if (rung.agent, rung.model) == (selected.agent, selected.model)), 0)
    routes = rungs[selected_index:]
    task = {"id": f"native:{agent_id}", "title": title, "agent": selected.agent,
            "model": selected.model, "status": "running", "output": "", "tools": []}
    native_agents[agent_id] = task

    async def announce() -> None:
        await send({"type": "subagent", "text": "", "session_id": None,
                    "is_error": task["status"] == "error",
                    "agent": task["agent"], "task": dict(task)})

    for index, route in enumerate(routes):
        task.update(agent=route.agent, model=route.model, status="running", output="")
        await announce()
        try:
            adapter = worker_factory(route.agent, route.model, route.effort, Scope(), workspace_dir)
            adapter.env["ANA_GATE_AGENT_ID"] = agent_id
            # docker exec does not inherit the Python subprocess environment.
            # Pass the task id into the workspace container as well, so the
            # hook attributes worker tool calls to this card.
            prefix = getattr(adapter, "command_prefix", None)
            if isinstance(prefix, list) and prefix[:2] == ["docker", "exec"]:
                prefix[-1:-1] = ["-e", f"ANA_GATE_AGENT_ID={agent_id}"]
            run = await _run_worker(adapter, prompt)
            error = run.error or ""
            output = run.output or ""
            failed = run.failed
            usage_tracker.record_tokens(route.agent, run.tokens, usd=float(run.cost_usd or 0.0))
        except Exception as exc:  # A worker failure can still move to the next rung.
            error, output, failed = str(exc), "", True
        error = mask_secrets(error, secrets or [])
        output = mask_secrets(output, secrets or [])
        if not failed:
            task.update(status="done", output=output, model=run.model or route.model)
            await announce()
            return {"status": "done", "output": output, "agent": route.agent}
        if index + 1 < len(routes) and is_recoverable_failure(f"{error}\n{output}"):
            next_route = routes[index + 1]
            record_failover(f"{route.agent}/{route.model}",
                            f"{next_route.agent}/{next_route.model}")
            continue
        task.update(status="error", output="", error="하위 작업을 완료하지 못했습니다.")
        await announce()
        return {"status": "error", "error": error or output or task["error"]}
    task.update(status="error", error="사용 가능한 하위 에이전트가 없습니다.")
    await announce()
    return {"status": "error", "error": task["error"]}
