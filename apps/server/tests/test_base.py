import pytest

from packages.adapters.base import AdapterEvent, AgentAdapter


def test_adapter_event_defaults():
    event = AdapterEvent(type="text", text="hi")
    assert event.session_id is None
    assert event.cost_usd is None
    assert event.is_error is False
    assert event.raw is None


def test_agent_adapter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        AgentAdapter()  # type: ignore[abstract]


def test_concrete_subclass_must_implement_both_methods():
    class Incomplete(AgentAdapter):
        async def terminate(self) -> None:
            return None

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
