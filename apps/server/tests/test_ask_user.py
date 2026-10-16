"""The agent asking the user: the MCP server, the gate endpoint, and the wait.

No CLI and no browser here — the protocol side is fed JSON lines, and the
endpoint side is driven against a registered gate session, which is exactly the
seam the two real clients meet.
"""
import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient

from apps.server.app.gate import lead_may_call
from apps.server.app.main import app
from packages.governance.session import Answer, GateRegistry
from packages.mcp.registry import LEAD, WORKER, load_registry
from packages.mcp.scope import scope_denial, server_for_tool
from packages.mcp.servers import ask_user


def _rpc(*messages: dict) -> list[dict]:
    """Runs the server over one batch of messages and returns its responses."""
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    stdout = io.StringIO()
    ask_user.serve(stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_initialize_answers_with_the_clients_protocol_version():
    """Both CLIs are newer than this file will be; refusing an unknown version
    would make the server vanish from a working setup."""
    responses = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2099-01-01"}}
    )

    assert responses[0]["result"]["protocolVersion"] == "2099-01-01"
    assert responses[0]["result"]["capabilities"] == {"tools": {}}


def test_notifications_get_no_response():
    assert _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_tools_list_offers_one_tool():
    responses = _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    tools = responses[0]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["ask_user"]
    assert tools[0]["inputSchema"]["required"] == ["questions"]


def test_an_unknown_method_is_an_error_not_a_crash():
    responses = _rpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})

    assert responses[0]["error"]["code"] == -32601


def test_garbage_input_is_skipped():
    stdout = io.StringIO()
    ask_user.serve(stdin=io.StringIO("not json\n\n"), stdout=stdout)
    assert stdout.getvalue() == ""


def test_calling_the_tool_posts_the_questions_and_pairs_up_the_answers(monkeypatch):
    sent = {}

    def fake_ask(questions):
        sent["questions"] = questions
        return {"answered": True, "answers": ["Postgres", ["a", "b"]]}

    monkeypatch.setattr(ask_user, "_ask", fake_ask)

    responses = _rpc(
        {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "ask_user",
                "arguments": {
                    "questions": [
                        {"question": "어느 DB를 쓸까?", "options": ["Postgres", "SQLite"]},
                        {"question": "무엇을 포함할까?", "options": ["a", "b"], "multi": True},
                    ]
                },
            },
        }
    )

    text = responses[0]["result"]["content"][0]["text"]
    assert sent["questions"][0]["question"] == "어느 DB를 쓸까?"
    # Paired with its question: an answer on its own is not usable by a model
    # that has written other things since.
    assert "Q: 어느 DB를 쓸까?\nA: Postgres" in text
    assert "A: a, b" in text


def test_an_unanswered_question_comes_back_as_a_result(monkeypatch):
    """A turn must not be lost because someone stepped away from the browser."""
    monkeypatch.setattr(
        ask_user, "_ask", lambda questions: {"answered": False, "note": "시간 초과"}
    )

    responses = _rpc(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "ask_user", "arguments": {"questions": [{"question": "?", "options": ["a", "b"]}]}}}
    )

    assert responses[0]["result"]["content"][0]["text"] == "시간 초과"
    assert "isError" not in responses[0]["result"]


def test_a_gate_that_cannot_be_reached_is_a_readable_failure(monkeypatch):
    def boom(questions):
        raise OSError("connection refused")

    monkeypatch.setattr(ask_user, "_ask", boom)

    responses = _rpc(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "ask_user", "arguments": {"questions": [{"question": "?", "options": ["a", "b"]}]}}}
    )

    assert responses[0]["result"]["isError"] is True
    assert "connection refused" in responses[0]["result"]["content"][0]["text"]


def test_question_without_choices_or_input_is_rejected_before_delivery(monkeypatch):
    monkeypatch.setattr(ask_user, "_ask", lambda _questions: pytest.fail("must not send"))
    responses = _rpc({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "ask_user", "arguments": {"questions": [{"question": "?"}]}},
    })
    assert responses[0]["result"]["isError"] is True
    assert "two choices or a labelled input" in responses[0]["result"]["content"][0]["text"]


def test_complex_question_schema_reaches_ask_server(monkeypatch):
    sent = {}

    def fake_ask(questions):
        sent["questions"] = questions
        return {"answered": True, "answers": ["fix", ["progress", "risks"], "직접 입력"]}

    monkeypatch.setattr(ask_user, "_ask", fake_ask)
    questions = [
        {"question": "할 일은?", "options": [
            {"value": "fix", "label": "수정", "description": "실패한 테스트 수정"},
            {"value": "docs", "label": "문서", "description": "설명 갱신"},
        ]},
        {"question": "무엇을 보고할까요?", "options": ["progress", "risks"],
         "multi": True, "required": False},
        {"question": "추가 의견은?", "input": {"label": "의견", "placeholder": "입력하세요"}},
    ]
    responses = _rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                      "params": {"name": "ask_user", "arguments": {"questions": questions}}})

    assert sent["questions"] == questions
    result = responses[0]["result"]
    assert "isError" not in result
    assert "A: progress, risks" in result["content"][0]["text"]
    assert "A: 직접 입력" in result["content"][0]["text"]


def test_worker_question_identifies_its_role(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"answered": false}'

    def urlopen(request, timeout):
        sent.update(json.loads(request.data))
        return Response()

    monkeypatch.setenv("ANA_GATE_ROLE", "worker")
    monkeypatch.setenv("ANA_GATE_TOKEN", "test-token")
    monkeypatch.setattr(ask_user.urllib.request, "urlopen", urlopen)
    ask_user._ask([{"question": "어느 쪽이 좋나요?"}])

    assert sent["role"] == "worker"


# --- the registry side ------------------------------------------------------


def test_the_ask_server_is_available_to_lead_and_worker():
    registry = load_registry()

    assert "ask" in registry.names_for(LEAD)
    assert "ask" in registry.names_for(WORKER)
    assert scope_denial("mcp__ask__ask_user", WORKER, registry.names_for(WORKER),
                        registry.names()) is None
    assert scope_denial("mcp__ask__ask_user", LEAD, registry.names_for(LEAD),
                        registry.names()) is None


def test_ask_server_receives_its_gate_token_explicitly():
    from packages.mcp.wiring import resolve_for_role

    servers, _ = resolve_for_role(
        WORKER, env={"ANA_GATE_TOKEN": "current-thread-token"}, check_command=False
    )
    ask = next(server for server in servers if server.name == "ask")
    assert ask.env["ANA_GATE_TOKEN"] == "current-thread-token"


def test_a_lead_process_is_started_with_the_lead_and_worker_servers():
    """process_role used to return WORKER alone, which meant a lead-scoped
    server was configured for nobody and never spawned at all."""
    from packages.mcp.wiring import process_role

    registry = load_registry()
    resolved, _ = registry.resolve(process_role(LEAD), check_command=False)
    names = {server.name for server in resolved}

    assert "ask" in names  # the lead's own
    assert "git" in names  # its native sub-agents'
    worker_only, _ = registry.resolve(process_role(WORKER), check_command=False)
    assert "ask" in {server.name for server in worker_only}


def test_the_lead_may_ask_but_still_may_not_work():
    """The lead does no work of its own; asking the user is one of the two
    exceptions, and hook payloads spell MCP tool names both ways."""
    assert lead_may_call("mcp__ask__ask_user") is True
    assert lead_may_call("askask_user") is True
    assert lead_may_call("request_user_input_async") is True
    assert lead_may_call("functions.request_user_input_async") is True
    assert lead_may_call("Bash") is False
    assert lead_may_call("mcp__git__git_log") is False
    assert server_for_tool("mcp__ask__ask_user", load_registry().names()) == "ask"


# --- the gate endpoint ------------------------------------------------------


@pytest.fixture
def registry_client(monkeypatch, postgres_dsn, tmp_path):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    with TestClient(app) as client:
        yield client


def test_ask_refuses_an_unknown_token(registry_client):
    response = registry_client.post(
        "/gate/ask",
        json={"questions": [{"question": "?"}]},
        headers={"x-ana-gate-token": "nope"},
    )
    assert response.status_code == 403


def test_ask_requires_a_question(registry_client):
    registry: GateRegistry = registry_client.app.state.gate_registry

    async def send(_payload):
        return None

    session = registry.register("t1", send)
    response = registry_client.post(
        "/gate/ask", json={"questions": []},
        headers={"x-ana-gate-token": session.token},
    )
    assert response.status_code == 422


def test_ask_requires_fixed_choices(registry_client):
    registry: GateRegistry = registry_client.app.state.gate_registry

    async def send(_payload):
        pytest.fail("invalid question must not reach the browser")

    session = registry.register("fixed-choices", send)
    response = registry_client.post(
        "/gate/ask", json={"questions": [{"question": "무엇을 할까요?"}]},
        headers={"x-ana-gate-token": session.token},
    )
    assert response.status_code == 422
    assert "two choices or a labelled input" in response.json()["detail"]


def test_ask_preserves_complex_question_details(registry_client, monkeypatch):
    registry: GateRegistry = registry_client.app.state.gate_registry
    session = registry.register("complex-question", lambda _payload: None)
    sent = {}

    async def answer(_session, payload):
        sent.update(payload)
        return Answer(value=["fix", ["progress", "risks"], "직접 입력"], by="user")

    monkeypatch.setattr(registry, "request_answer", answer)
    response = registry_client.post(
        "/gate/ask",
        headers={"x-ana-gate-token": session.token},
        json={"questions": [
            {"question": "할 일은?", "options": [
                {"value": "fix", "label": "수정", "description": "테스트를 고칩니다"},
                {"value": "docs", "label": "문서"},
            ]},
            {"question": "보고할 내용은?", "options": ["progress", "risks"],
             "multi": True, "required": False},
            {"question": "추가 의견은?", "input": {"label": "의견"}},
        ]},
    )
    assert response.status_code == 200
    assert sent["questions"][0]["options"][0]["description"] == "테스트를 고칩니다"
    assert sent["questions"][1]["multi"] is True
    assert sent["questions"][1]["required"] is False
    assert sent["questions"][2]["input"]["label"] == "의견"
    assert response.json()["answers"] == ["fix", ["progress", "risks"], "직접 입력"]


@pytest.mark.asyncio
async def test_request_answer_returns_what_the_browser_sent():
    registry = GateRegistry()
    sent: list[dict] = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t2", send)

    import asyncio

    async def answer_soon():
        # Waits for the frame to have gone out, then answers it the way the
        # WebSocket handler does.
        while not sent:
            await asyncio.sleep(0.01)
        registry.resolve_answer(session, sent[0]["request_id"], ["Postgres"])

    answers, _ = await asyncio.gather(
        registry.request_answer(session, {"questions": [{"question": "어느 DB?"}]}),
        answer_soon(),
    )

    assert answers.value == ["Postgres"]
    # Who answered rides along, for the transcript and the audit row.
    assert answers.by == "user"
    assert sent[0]["type"] == "question"
    # The pending entry is cleaned up, so a late second answer is a no-op.
    assert session.pending == {}
    assert registry.resolve_answer(session, sent[0]["request_id"], ["again"]) is False


@pytest.mark.asyncio
async def test_browser_answer_requires_matching_count_and_required_values():
    registry = GateRegistry()
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("answer-validation", send)
    waiting = asyncio.create_task(registry.request_answer(session, {"questions": [
        {"question": "Required?"}, {"question": "Optional?", "required": False},
    ]}))
    async with asyncio.timeout(2):
        while not sent:
            await asyncio.sleep(0.01)
    request_id = sent[0]["request_id"]

    assert registry.resolve_answer(session, request_id, ["yes"]) is False
    assert registry.resolve_answer(session, request_id, ["  ", "optional"]) is False
    assert registry.resolve_answer(session, request_id, [[], "optional"]) is False
    assert request_id in session.pending
    assert registry.resolve_answer(session, request_id, ["yes", ""]) is True
    assert (await asyncio.wait_for(waiting, 2)).value == ["yes", ""]


@pytest.mark.asyncio
async def test_an_unanswered_question_times_out_as_none():
    registry = GateRegistry(question_timeout=0.05)

    async def send(_payload):
        return None

    session = registry.register("t3", send)

    assert (await registry.request_answer(session, {"questions": [{"question": "?"}]})).value is None


@pytest.mark.asyncio
async def test_a_closed_connection_ends_the_wait():
    """Anything parked on a connection that goes away must not hang until the
    timeout: the hook and the tool call both need an answer now."""
    registry = GateRegistry(question_timeout=30)

    async def send(_payload):
        return None

    session = registry.register("t4", send)

    import asyncio

    async def close_soon():
        while not session.pending:
            await asyncio.sleep(0.01)
        registry.unregister(session.token)

    answers, _ = await asyncio.gather(
        registry.request_answer(session, {"questions": [{"question": "?"}]}),
        close_soon(),
    )
    assert answers.value is None
