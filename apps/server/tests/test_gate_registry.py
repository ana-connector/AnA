"""The approval broker: tokens, waiting, and every path that must deny."""
import asyncio

import pytest

from packages.governance.session import GateRegistry


def _registry(timeout: float = 0.2) -> GateRegistry:
    return GateRegistry(approval_timeout=timeout)


@pytest.mark.asyncio
async def test_tokens_are_unique_and_resolve_to_their_thread():
    registry = _registry()

    async def send(_):
        return None

    a = registry.register("thread-a", send)
    b = registry.register("thread-b", send)

    assert a.token != b.token
    assert registry.get(a.token).thread_id == "thread-a"
    assert registry.get("nonsense") is None


@pytest.mark.asyncio
async def test_approval_returns_the_users_answer():
    registry = _registry(timeout=5)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {"tool_name": "Bash"}))
    await asyncio.sleep(0.05)

    assert registry.resolve_approval(session, sent[0]["request_id"], "allow") is True
    assert await task == "allow"


@pytest.mark.asyncio
async def test_unanswered_approval_times_out():
    registry = _registry(timeout=0.2)

    async def send(_):
        return None

    session = registry.register("t", send)

    assert await registry.request_approval(session, {}) == "timeout"


@pytest.mark.asyncio
async def test_failed_send_denies():
    registry = _registry(timeout=5)

    async def send(_):
        raise ConnectionError("socket gone")

    session = registry.register("t", send)

    # The browser could not even be asked, so nothing was approved.
    assert await registry.request_approval(session, {}) == "deny"


@pytest.mark.asyncio
async def test_closing_a_session_denies_anything_still_waiting():
    registry = _registry(timeout=30)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    await asyncio.sleep(0.05)

    registry.unregister(session.token)

    # Denied immediately rather than hanging until the timeout: nobody is
    # left who could answer.
    assert await task == "deny"
    assert registry.get(session.token) is None


@pytest.mark.asyncio
async def test_duplicate_answer_is_a_no_op():
    registry = _registry(timeout=5)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    await asyncio.sleep(0.05)
    request_id = sent[0]["request_id"]

    assert registry.resolve_approval(session, request_id, "allow") is True
    assert await task == "allow"
    # A second click (or a stale card) must not resolve anything.
    assert registry.resolve_approval(session, request_id, "deny") is False


@pytest.mark.asyncio
async def test_non_allow_answers_are_treated_as_deny():
    registry = _registry(timeout=5)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    await asyncio.sleep(0.05)
    registry.resolve_approval(session, sent[0]["request_id"], "whatever")

    assert await task == "deny"
