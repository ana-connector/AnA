"""Planning step: the lead agent decides whether to split a request.

The routing rule ("which CLI should do this?") lives in the prompt rather than
in Python, per the roadmap: the lead judges the task, ANA executes the plan it
gets back. Python's job is to parse that answer safely — a model's JSON arrives
wrapped in prose, fenced, truncated, or with fields it invented.
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from packages.governance.subagent_scope import Scope
from packages.orchestrator.handoff import AGENTS, DEFAULT_AGENT

DIRECT = "direct"
DELEGATE = "delegate"

DEFAULT_MAX_TASKS = 5

PLAN_INSTRUCTIONS = """\
너는 오케스트레이터의 대장 에이전트다. 아래 요청을 보고 **JSON만** 출력해라.
설명, 코드펜스 밖 텍스트, 사과문 금지.

도구 없이, 네가 아는 것과 지금까지의 대화만으로 답할 수 있는 요청이면:
{"mode": "direct"}

기계를 건드려야 하는 요청이면(파일 읽기/쓰기, 명령 실행, 코드 조사, 검색 등):
{"mode": "delegate", "tasks": [
  {"title": "짧은 제목", "agent": "codex", "prompt": "그 작업만 수행하도록 쓴 완결된 지시",
   "scope": {"mode": "write", "paths": []}, "verify": "pytest -q tests/test_x.py"}
]}

규칙:
- agent는 "claude", "codex", "kimi" 중 하나. 코드 생성/리팩터링/구현은 codex,
  설명/리뷰/조사/기획은 claude, 긴 파일·대용량 맥락을 통째로 훑는 작업은 kimi.
- scope는 그 하위 에이전트에게 줄 권한이다(생략 가능, 기본은 전체 쓰기).
  - mode: "read"면 파일 수정·명령 실행이 차단된다. 조사/리뷰/설명 작업은 read로 줘라.
  - paths: 워크스페이스 기준 상대 경로 목록. 적으면 그 안에서만 작업할 수 있다.
  - 필요 이상으로 넓게 주지 마라. 좁게 준 스코프는 게이트가 강제한다.
- verify는 그 작업이 끝났음을 증명할 셸 명령이다(생략 가능). ANA가 직접 실행하고,
  실패하면 실패 출력을 붙여 그 작업을 한 번 다시 시킨다. 하위 에이전트의 "다 했다"는
  말은 검증 결과를 이기지 못한다.
  - 코드 변경이면 테스트/빌드/린트 명령을 써라. 없으면 비워라(없는 명령을 지어내지 마라).
  - 조사·설명 작업에는 붙이지 마라.
- 작업은 최대 %(max_tasks)d개. 서로 독립적이어야 한다(동시에 실행된다).
- 각 prompt는 그 작업만 보고도 수행 가능하도록 자족적으로 써라.
- **direct는 "쉬운 요청"이 아니라 "도구가 필요 없는 요청"에 쓴다.** 너는 파일도
  못 읽고 명령도 못 돌린다. 파일 내용·실행 결과·저장소 상태가 답에 필요하면
  작업이 아무리 작아도 delegate로, 작업 1개짜리 계획을 만들어라.
- 반대로 개념 설명, 방금 대화 정리, 네가 이미 아는 것으로 끝나는 질문은
  delegate하지 마라 — 하위 에이전트를 띄우는 것도 비용이다.
- 병렬로 나눌 이유가 없으면 작업 1개짜리 delegate가 정답이다. 억지로 쪼개지 마라.

요청:
%(prompt)s
"""

SYNTHESIS_INSTRUCTIONS = """\
아래는 네가 세운 계획에 따라 서브에이전트들이 병렬로 수행한 결과다.
계획 단계는 끝났다. **지금은 JSON을 쓰지 마라.** 사용자에게 그대로 보여줄 한국어
산문으로 최종 답변을 작성해라. 새 계획이나 작업 목록을 만들지 마라.
실패한 작업이 있으면 숨기지 말고 무엇이 안 됐는지 밝혀라.

괄호 안의 검증/병합 결과는 ANA가 직접 확인한 사실이다. 하위 에이전트가 "다 했다"고
써도 검증이 실패했으면 그 작업은 실패한 것으로 보고해라.

원래 요청:
%(prompt)s

서브에이전트 결과:
%(results)s
"""


@dataclass(frozen=True)
class PlanTask:
    id: str
    title: str
    prompt: str
    agent: str = DEFAULT_AGENT
    # What the lead is willing to let this task do (roadmap stage 3's
    # "권한 스코프"). Enforced at the gate, not by the CLI.
    scope: Scope = field(default_factory=Scope)
    # A shell command that proves the task is done. ANA runs it itself after
    # the worker finishes (packages/orchestrator/verification.py); empty means
    # there is nothing to check, which is the honest state for a task whose
    # output is prose.
    verify: str = ""


@dataclass(frozen=True)
class Plan:
    mode: str = DIRECT
    tasks: list[PlanTask] = field(default_factory=list)
    # Why the plan ended up direct, when that was not the model's choice.
    note: str = ""

    @property
    def delegating(self) -> bool:
        return self.mode == DELEGATE and bool(self.tasks)


def max_tasks() -> int:
    """Also the concurrency cap: planning more tasks than can run at once only
    queues them, and the roadmap fixes the cap at 5 by default."""
    try:
        return max(1, int(os.environ.get("ANA_MAX_SUBAGENTS", DEFAULT_MAX_TASKS)))
    except ValueError:
        return DEFAULT_MAX_TASKS


def build_planning_prompt(prompt: str, limit: Optional[int] = None) -> str:
    return PLAN_INSTRUCTIONS % {"max_tasks": limit or max_tasks(), "prompt": prompt}


def build_synthesis_prompt(prompt: str, results: list[dict]) -> str:
    rendered = "\n\n".join(
        f"[{r['title']} · {r['agent']} · {r['status']}]{_verdict(r)}\n"
        f"{(r.get('output') or '').strip()[:4000]}"
        for r in results
    )
    return SYNTHESIS_INSTRUCTIONS % {"prompt": prompt, "results": rendered or "(결과 없음)"}


def _verdict(result: dict) -> str:
    """What ANA itself observed about a task, appended to the worker's own
    words. A sub-agent that says "done" after a failing test is reporting a
    claim; this line is the fact, and the lead is told to prefer it."""
    parts = []
    check = result.get("verification") or {}
    if check.get("ran"):
        parts.append(
            f"검증 {'통과' if check.get('passed') else '실패'}: {check.get('command', '')}"
            + ("" if check.get("passed") else f" — {check.get('reason', '')}")
        )
    elif check.get("reason"):
        parts.append(f"검증 실행 못함: {check['reason']}")
    merge = result.get("merge") or {}
    if merge and not merge.get("merged", True):
        parts.append(f"병합 충돌 — 결과는 {merge.get('branch', '')} 브랜치에 있음")
    elif merge.get("empty"):
        parts.append("변경 없음")
    return f" ({'; '.join(parts)})" if parts else ""


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> Optional[dict]:
    """Finds the JSON object in a model answer.

    Tries the whole string first, then the outermost braces — which covers a
    fenced block and prose on either side without special-casing either."""
    for candidate in (text.strip(), None):
        if candidate is None:
            match = _JSON_BLOCK_RE.search(text)
            if match is None:
                return None
            candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_plan(text: str, limit: Optional[int] = None) -> Plan:
    """Turns the lead's answer into a plan.

    Anything unparseable, empty, or not actually a delegation falls back to a
    direct turn: a planning step that fails should cost the user nothing but a
    little latency, never an error."""
    cap = limit or max_tasks()
    payload = _extract_json(text or "")
    if payload is None:
        return Plan(note="계획을 JSON으로 읽지 못해 단독 처리")
    if payload.get("mode") != DELEGATE:
        return Plan()

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return Plan(note="위임 계획에 작업이 없어 단독 처리")

    tasks: list[PlanTask] = []
    for index, raw in enumerate(raw_tasks[:cap]):
        if not isinstance(raw, dict):
            continue
        task_prompt = raw.get("prompt")
        if not isinstance(task_prompt, str) or not task_prompt.strip():
            continue
        agent = raw.get("agent")
        tasks.append(
            PlanTask(
                id=f"t{index + 1}",
                title=str(raw.get("title") or f"작업 {index + 1}")[:80],
                prompt=task_prompt.strip(),
                scope=Scope.parse(raw.get("scope")),
                verify=(raw.get("verify") or "").strip()[:500]
                if isinstance(raw.get("verify"), str)
                else "",
                # Client-of-the-model data: an invented agent name would
                # otherwise pick a subprocess that does not exist.
                agent=agent if agent in AGENTS else DEFAULT_AGENT,
            )
        )

    if not tasks:
        return Plan(note="실행 가능한 작업이 없어 단독 처리")
    dropped = len(raw_tasks) - len(tasks)
    return Plan(
        mode=DELEGATE,
        tasks=tasks,
        note=f"{dropped}개 작업은 상한/형식 때문에 제외" if dropped > 0 else "",
    )
