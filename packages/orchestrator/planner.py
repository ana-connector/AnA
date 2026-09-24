"""Planning step: the lead agent decides whether to split a request.

The lead describes tasks and dependencies; the worker routing ladder chooses
their models at execution time. Python parses the plan safely — a model's JSON arrives
wrapped in prose, fenced, truncated, or with fields it invented.
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from packages.governance.subagent_scope import Scope
from packages.orchestrator.handoff import AGENTS

DIRECT = "direct"
DELEGATE = "delegate"

# How many tasks one plan may contain, and how many may run at once. They
# stopped being the same number when tasks gained dependencies: a five-stage
# pipeline is five tasks that run two at a time, and capping the *plan* at the
# concurrency limit truncated exactly the plans worth making.
DEFAULT_MAX_TASKS = 10
DEFAULT_MAX_CONCURRENCY = 5
# How many planning rounds one turn may use: the first plan, plus this many
# chances to extend it once the results are in. A plan written before anything
# ran is a guess about what the work will turn out to need, and the useful
# version of "dynamic" is being allowed to add the step the first round
# discovered — bounded, because a lead that can always add one more task can
# also never finish.
DEFAULT_PLAN_ROUNDS = 2

# Written in English, unlike the answer it leads to: this prompt, every task
# instruction and every handoff brief are read only by another model, and the
# same words cost noticeably fewer tokens in English. What the *user* reads —
# the final answer, the task titles the UI shows, the questions the lead asks —
# stays Korean, and the instructions below say so explicitly.
PLAN_INSTRUCTIONS = """\
You are the lead agent of an orchestrator. Read the request below and output
**JSON only** — no prose, no text outside the object, no apologies.

If you can answer from what you already know and this conversation, with no
tools at all:
{"mode": "direct"}

If the request needs the machine (reading or writing files, running commands,
inspecting code, searching):
{"mode": "delegate", "tasks": [
  {"id": "t1", "title": "짧은 제목", "needs": [],
   "prompt": "a self-contained instruction for this task alone",
   "scope": {"mode": "write", "paths": []}, "verify": "pytest -q tests/test_x.py"},
  {"id": "t2", "title": "다음 단계", "needs": ["t1"],
   "prompt": "what to do with t1's result", "skill": "code-review"}
], "verify": "docker compose config -q"}

Rules:
- Do not set `agent`. ANA chooses each worker's CLI and model automatically
  from the worker routing ladder when the task starts.
- `title` is what the user sees in the UI, so **write titles in Korean**.
  `prompt` is read by a sub-agent, so write it in English.
- `scope` is the permission you grant that sub-agent (optional, defaults to full
  write access).
  - mode "read" blocks file edits and command execution. Give investigation,
    review and explanation tasks "read".
  - paths: workspace-relative paths. Listed, the task may only work inside them.
  - Do not grant more than the task needs. A narrow scope is enforced at the gate.
- `verify` is a shell command that proves the task is done (optional). ANA runs
  it itself and, if it fails, re-dispatches that one task once with the failure
  output attached. A sub-agent saying "done" does not outrank the check.
  - For code changes use the test, build or lint command. Leave it empty if there
    is none — do not invent a command that does not exist.
  - Do not attach one to investigation or explanation tasks.
- The outermost `verify` runs **once, after every task has finished and merged**
  (optional). Per-task checks each pass on their own branch, so put a command
  here if there is one that shows the whole thing still works.
- `skill` names an ANA skill for that task (optional). Use only names from the
  skill list in your system prompt; the skill's procedure is prepended to the
  sub-agent's instruction.
- At most %(max_tasks)d tasks. (ANA caps how many run at once separately.)
- **Express order with `needs`.** Tasks with empty `needs` start together; a task
  with `needs` starts when all of them have finished, and their results are
  attached to its instruction automatically. Do not collapse a sequence into one
  task — split it per step and wire it with `needs`.
  Example: plan(t1) → backend(t2, needs t1) / design(t3, needs t1) →
  frontend(t4, needs t3) → deploy(t5, needs t2+t4). t2 and t3 run in parallel.
- Keep ids short (t1, t2 …). `needs` may only name ids you already defined, and
  cycles are rejected.
- Write each `prompt` so the task can be carried out from that text alone.
- **"direct" means "needs no tools", not "easy".** You cannot read files or run
  commands. If the answer depends on file contents, command output or repository
  state, delegate — even a one-task plan.
- Conversely, do not delegate concept explanations, summaries of what was just
  said, or anything you already know: starting a sub-agent costs something too.
- If there is no reason to split, a one-task plan is the right answer. Do not
  break work up for the sake of it.
- Plan the steps you can actually see now. When these finish you will be shown
  their results and asked whether anything else is needed, so you do not have to
  guess at follow-up work the first round will reveal.
- If the request is genuinely ambiguous — two readings that lead to different
  work, a choice only the user can make — call `ask_user` **before** planning,
  in Korean, and plan once you have the answer. Do not ask about anything a
  sub-agent could find out by reading the repository.

Request:
%(prompt)s
"""

SYNTHESIS_INSTRUCTIONS = """\
Below is what the sub-agents produced for the plan you made. The planning phase
is over: **do not output JSON** and do not make a new plan or task list.

Write the final answer for the user **in Korean**, as prose they can read as-is.
If a task failed, say what failed and why rather than hiding it.

Anything in parentheses is a fact ANA checked itself. A sub-agent may have
written "done" while the check failed — report that task as failed.

Original request:
%(prompt)s

Sub-agent results:
%(results)s
"""


REPLAN_INSTRUCTIONS = """\
These are the results of the tasks you planned. Decide what happens next and
output **JSON only**.

Nothing further is needed — ANA will ask you for the user's answer next:
{"mode": "done"}

Something the results revealed still has to be done:
{"mode": "delegate", "tasks": [
  {"id": "t9", "title": "짧은 제목", "needs": ["t2"],
   "prompt": "a self-contained instruction", "verify": ""}
]}

Rules:
- Same task format as the original plan: no `agent`, Korean `title`, English `prompt`,
  optional `scope`, `verify` and `skill`.
- Ids already used, which you may name in `needs` but must not reuse:
  %(existing_ids)s
- `needs` may reference a finished task; its result is attached automatically.
- Add a task **only because of what these results showed**: a dependency nobody
  could see before running, a failure that needs a different approach, a
  follow-up step the first plan could not know about. Do not re-run work that is
  done, and do not add polish nobody asked for.
- At most %(max_tasks)d more tasks.
- If the original plan covered the request, answer {"mode": "done"}. That is the
  normal answer, not a failure.

Original request:
%(prompt)s

Results so far:
%(results)s
"""


@dataclass(frozen=True)
class PlanTask:
    id: str
    title: str
    prompt: str
    agent: str = "auto"
    # What the lead is willing to let this task do (roadmap stage 3's
    # "권한 스코프"). Enforced at the gate, not by the CLI.
    scope: Scope = field(default_factory=Scope)
    # Ids of the tasks that must finish first. Empty means "start immediately";
    # everything with an empty `needs` runs in parallel.
    needs: tuple[str, ...] = ()
    # An ANA skill this task should follow, by name. Validated against the
    # library, so a name the model invented is dropped rather than pasted into
    # a prompt as an instruction nobody wrote.
    skill: str = ""
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
    # A command ANA runs once, after every task has merged. Per-task checks
    # each pass on their own branch; this is the one that catches two of them
    # being fine separately and broken together.
    verify: str = ""

    @property
    def delegating(self) -> bool:
        return self.mode == DELEGATE and bool(self.tasks)


def max_tasks() -> int:
    """How many tasks a plan may contain (ANA_MAX_PLAN_TASKS)."""
    try:
        return max(1, int(os.environ.get("ANA_MAX_PLAN_TASKS", DEFAULT_MAX_TASKS)))
    except ValueError:
        return DEFAULT_MAX_TASKS


def max_concurrency() -> int:
    """How many sub-agents may run at the same time (ANA_MAX_SUBAGENTS).

    The roadmap's resource cap, unchanged at 5: a plan may be longer than this
    — the rest simply waits its turn, which for a dependency graph is what it
    was going to do anyway."""
    try:
        return max(1, int(os.environ.get("ANA_MAX_SUBAGENTS", DEFAULT_MAX_CONCURRENCY)))
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY


def build_planning_prompt(prompt: str, limit: Optional[int] = None) -> str:
    return PLAN_INSTRUCTIONS % {"max_tasks": limit or max_tasks(), "prompt": prompt}


def plan_rounds() -> int:
    """How many planning rounds one turn may use (ANA_PLAN_ROUNDS).

    2 by default: plan, run, then one chance to add what the results revealed.
    Each extra round costs one short planning call whether or not it adds
    anything, which is why this is 2 and not 5. 1 restores the old behavior —
    plan once, run, answer."""
    try:
        return max(1, int(os.environ.get("ANA_PLAN_ROUNDS", DEFAULT_PLAN_ROUNDS)))
    except ValueError:
        return DEFAULT_PLAN_ROUNDS


def build_replan_prompt(
    prompt: str,
    results: list[dict],
    existing_ids: Iterable[str],
    limit: Optional[int] = None,
) -> str:
    """The question ANA puts to the lead between rounds: given what came back,
    is the plan finished or does it need another step?

    The same JSON parser reads the answer, so "done" needs no special case —
    anything that is not a delegation with tasks ends the fan-out."""
    rendered = "\n\n".join(
        f"[{r.get('id', '?')} · {r.get('title', '')} · {r.get('status', '')}]{_verdict(r)}\n"
        f"{r.get('output') or ''}"
        for r in results
    )
    return REPLAN_INSTRUCTIONS % {
        "max_tasks": limit or max_tasks(),
        "existing_ids": ", ".join(existing_ids) or "(none)",
        "prompt": prompt,
        "results": rendered or "(no results)",
    }


_SKILL_PLACEHOLDER_RE = re.compile(r"\{\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}\}")

SKILL_INSTRUCTIONS = """\
[SKILL: %(name)s] Follow this procedure for this task.

%(instructions)s

[TASK]
%(prompt)s
"""


def _with_skill(task) -> str:
    """The task's own instruction, preceded by the skill it was assigned."""
    if not task.skill:
        return task.prompt
    try:
        from packages.skills.loader import load_library

        skill = load_library().get(task.skill)
    except Exception:  # noqa: BLE001 - see _parse_skill
        skill = None
    if skill is None:
        return task.prompt
    # The skill's input placeholders are left unfilled on purpose — the plan
    # supplies the specifics in the task prompt, and "{{target}}" reaching an
    # agent verbatim reads as a template ANA forgot to finish.
    procedure = _SKILL_PLACEHOLDER_RE.sub("(see the task instruction below)", skill.instructions.strip())
    return SKILL_INSTRUCTIONS % {
        "name": skill.name,
        "instructions": procedure,
        "prompt": task.prompt,
    }


UPSTREAM_INSTRUCTIONS = """\
[UPSTREAM RESULTS] What the tasks yours waited for actually produced. Continue
from these as given; do not redo work that is already done.

%(results)s

[YOUR TASK]
%(prompt)s
"""


def build_task_prompt(task, upstream: list[dict]) -> str:
    """One task's instruction, with its skill and what it waited for in front.

    A dependency that carried no output is still listed: "the previous step
    produced nothing" is information, and a worker left to assume otherwise
    invents the handover."""
    prompt = _with_skill(task)
    if not upstream:
        return prompt
    rendered = "\n\n".join(
        f"[{entry['title']} · {entry['status']}]\n"
        f"{entry.get('output') or '(no output)'}"
        for entry in upstream
    )
    return UPSTREAM_INSTRUCTIONS % {"results": rendered, "prompt": prompt}


def build_synthesis_prompt(prompt: str, results: list[dict]) -> str:
    rendered = "\n\n".join(
        f"[{r['title']} · {r['agent']} · {r['status']}]{_verdict(r)}\n"
        f"{r.get('output') or ''}"
        for r in results
    )
    return SYNTHESIS_INSTRUCTIONS % {"prompt": prompt, "results": rendered or "(no results)"}


def _verdict(result: dict) -> str:
    """What ANA itself observed about a task, appended to the worker's own
    words. A sub-agent that says "done" after a failing test is reporting a
    claim; this line is the fact, and the lead is told to prefer it."""
    parts = []
    check = result.get("verification") or {}
    if check.get("ran"):
        parts.append(
            f"check {'passed' if check.get('passed') else 'FAILED'}: {check.get('command', '')}"
            + ("" if check.get("passed") else f" — {check.get('reason', '')}")
        )
    elif check.get("reason"):
        parts.append(f"check could not run: {check['reason']}")
    merge = result.get("merge") or {}
    if merge and not merge.get("merged", True):
        parts.append(f"merge conflict — the work is on branch {merge.get('branch', '')}")
    elif merge.get("empty"):
        parts.append("no changes")
    return f" ({'; '.join(parts)})" if parts else ""


def _parse_skill(raw) -> str:
    """A skill name the library actually has, or "".

    Checked here rather than at use: a name the model invented would otherwise
    reach the worker as "follow the X procedure" with no X to follow, which
    reads to the agent as an instruction it failed to understand."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    name = raw.strip()
    try:
        from packages.skills.loader import load_library

        return name if load_library().get(name) is not None else ""
    except Exception:  # noqa: BLE001 - a broken library is not a broken plan
        return ""


def _parse_needs(
    raw, id_map: dict[str, str], own_id: str, known_ids: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Dependencies as ANA's own task ids.

    Client-of-the-model data: an id nobody defined, or a task naming itself,
    is dropped rather than failing the plan — a dependency ANA cannot resolve
    would otherwise block the task forever.

    `known_ids` are tasks from an earlier round of the same turn. They are
    already ANA's own ids, so a later plan may name them directly, and their
    results are carried into the new task's prompt."""
    if not isinstance(raw, list):
        return ()
    needs = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        name = entry.strip()
        resolved = id_map.get(name) or (name if name in known_ids else None)
        if resolved and resolved != own_id and resolved not in needs:
            needs.append(resolved)
    return tuple(needs)


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


def parse_plan(
    text: str,
    limit: Optional[int] = None,
    *,
    id_offset: int = 0,
    known_ids: Iterable[str] = (),
) -> Plan:
    """Turns the lead's answer into a plan.

    Anything unparseable, empty, or not actually a delegation falls back to a
    direct turn: a planning step that fails should cost the user nothing but a
    little latency, never an error.

    ANA assigns the ids, not the model — so a plan that numbers its tasks its own
    way still wires its dependencies to the right ones. `id_offset` continues the
    numbering for a later round of the same turn (t3, t4 … after t1 and t2), and
    `known_ids` are the tasks from earlier rounds that a new one may depend on."""
    cap = limit or max_tasks()
    known = tuple(known_ids)
    payload = _extract_json(text or "")
    if payload is None:
        return Plan(note="계획을 JSON으로 읽지 못해 단독 처리")
    if payload.get("mode") != DELEGATE:
        return Plan()

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return Plan(note="위임 계획에 작업이 없어 단독 처리")

    tasks: list[PlanTask] = []
    # The model's own ids, in order, so `needs` can be resolved to the ids ANA
    # assigns. A plan that numbers its tasks differently than ANA does would
    # otherwise wire the dependencies to the wrong ones.
    id_map: dict[str, str] = {}
    for index, raw in enumerate(raw_tasks[:cap]):
        if isinstance(raw, dict):
            claimed = raw.get("id")
            if isinstance(claimed, str) and claimed.strip():
                id_map[claimed.strip()] = f"t{id_offset + index + 1}"

    for index, raw in enumerate(raw_tasks[:cap]):
        if not isinstance(raw, dict):
            continue
        task_prompt = raw.get("prompt")
        if not isinstance(task_prompt, str) or not task_prompt.strip():
            continue
        agent = raw.get("agent")
        tasks.append(
            PlanTask(
                needs=_parse_needs(
                    raw.get("needs"), id_map, f"t{id_offset + index + 1}", known
                ),
                id=f"t{id_offset + index + 1}",
                title=str(raw.get("title") or f"작업 {id_offset + index + 1}")[:80],
                prompt=task_prompt.strip(),
                scope=Scope.parse(raw.get("scope")),
                verify=(raw.get("verify") or "").strip()[:500]
                if isinstance(raw.get("verify"), str)
                else "",
                skill=_parse_skill(raw.get("skill")),
                # Client-of-the-model data: an invented agent name would
                # otherwise pick a subprocess that does not exist.
                agent=agent if agent in AGENTS else "auto",
            )
        )

    if not tasks:
        return Plan(note="실행 가능한 작업이 없어 단독 처리")
    dropped = len(raw_tasks) - len(tasks)
    turn_verify = payload.get("verify")
    return Plan(
        mode=DELEGATE,
        tasks=tasks,
        note=f"{dropped}개 작업은 상한/형식 때문에 제외" if dropped > 0 else "",
        verify=turn_verify.strip()[:500] if isinstance(turn_verify, str) else "",
    )
