"""Circuit breaker logic: what counts as a usage limit, and what to try next."""
import pytest

from packages.orchestrator.breaker import (
    BreakerConfig,
    BreakerState,
    Rung,
    build_chain,
    is_usage_limit,
    next_rung,
    rate_limit_exceeded,
    thread_budget_exceeded,
)


@pytest.mark.parametrize(
    "text",
    [
        "Claude AI usage limit reached",
        "API Error: 429 Too Many Requests",
        "your credit balance is too low",
        "rate_limit_error",
        "Error: quota exceeded for this model",
        "server overloaded, try again",
    ],
)
def test_usage_limit_texts_are_recognized(text):
    assert is_usage_limit(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "SyntaxError: invalid syntax",
        "file not found",
        "Command blocked by PreToolUse hook: ANA governance rule",
    ],
)
def test_ordinary_failures_are_not_usage_limits(text):
    # A wrong request must surface as an error, not silently cost a retry on
    # another provider.
    assert is_usage_limit(text) is False


def test_rate_limit_windows_at_the_threshold_count():
    usage = {"unifiedWindows": {"five_hour": {"utilization": 0.97}}}

    assert rate_limit_exceeded(usage, 0.95) is True


def test_rate_limit_below_threshold_does_not_trip():
    usage = {"unifiedWindows": {"five_hour": {"utilization": 0.4}}}

    assert rate_limit_exceeded(usage, 0.95) is False
    assert rate_limit_exceeded(None, 0.95) is False
    assert rate_limit_exceeded({"unifiedWindows": "nonsense"}, 0.95) is False


def test_any_exhausted_window_trips():
    usage = {
        "unifiedWindows": {
            "five_hour": {"utilization": 0.1},
            "seven_day": {"utilization": 0.99},
        }
    }

    assert rate_limit_exceeded(usage, 0.95) is True


def test_chain_tries_the_other_auth_mode_before_the_other_cli():
    chain = build_chain("claude", "api")

    # The two Claude modes bill against different buckets, so re-auth is the
    # cheapest escape; switching CLI costs the conversation its session.
    assert [r.label for r in chain] == ["claude/api", "claude/pro", "codex", "kimi"]


def test_chain_from_pro_mode():
    assert [r.label for r in build_chain("claude", "pro")] == [
        "claude/pro",
        "claude/api",
        "codex",
        "kimi",
    ]


def test_chain_from_codex_falls_back_to_claude():
    # Nothing to re-auth on the Codex side.
    assert [r.label for r in build_chain("codex", "pro")] == ["codex", "claude/pro", "kimi"]


def test_next_rung_skips_what_was_tried():
    chain = build_chain("claude", "api")

    assert next_rung(chain, ["claude/api"]).label == "claude/pro"
    assert next_rung(chain, ["claude/api", "claude/pro"]).label == "codex"
    assert next_rung(chain, ["claude/api", "claude/pro", "codex"]).label == "kimi"
    assert next_rung(chain, ["claude/api", "claude/pro", "codex", "kimi"]) is None


def test_breaker_state_expires_after_the_cooldown():
    state = BreakerState(rung_label="codex", tripped_at=1000.0, reason="limit")

    assert state.active(cooldown_seconds=900, now=1500.0) is True
    # Recovery is just the cooldown expiring — no probe, no scheduler.
    assert state.active(cooldown_seconds=900, now=2000.0) is False


def test_unset_breaker_state_is_never_active():
    assert BreakerState().active(cooldown_seconds=900, now=10.0) is False


def test_thread_budget_trips_on_cost_and_tokens():
    config = BreakerConfig(max_thread_cost_usd=1.0, max_thread_tokens=1000)

    assert thread_budget_exceeded(config, 1.5, 0) is not None
    assert thread_budget_exceeded(config, 0.0, 2000) is not None
    assert thread_budget_exceeded(config, 0.5, 500) is None


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("ANA_MAX_THREAD_COST_USD", "2.5")
    monkeypatch.setenv("ANA_FAILOVER_COOLDOWN_SECONDS", "60")

    config = BreakerConfig.from_env()

    assert config.max_thread_cost_usd == 2.5
    assert config.cooldown_seconds == 60


def test_rung_label_without_auth_mode():
    assert Rung("codex").label == "codex"
