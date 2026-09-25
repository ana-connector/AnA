"""Automatic agent routing and the usage-window tracker."""
import textwrap
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.observability.limits import FIVE_HOURS, SEVEN_DAYS, UsageTracker
from packages.orchestrator.routing import LEAD, WORKER, ModelLadder, decide


LADDER = """
lead:
  - agent: claude
    model: fable
    until: {window: five_hour, utilization: 0.25}
  - agent: codex
    model: gpt-6-astra
    until: {window: five_hour, utilization: 0.25}
  - agent: claude
    model: opus
workers:
  default:
    - agent: codex
      model: gpt-6-astra
      until: {window: five_hour, utilization: 0.9}
    - agent: claude
      model: opus
"""


@pytest.fixture
def ladder(tmp_path, monkeypatch):
    path = tmp_path / "routing.yaml"
    path.write_text(LADDER, encoding="utf-8")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(path))
    return path


def _usage(**utilization) -> dict:
    """A snapshot shaped like UsageTracker's, with five-hour utilization set
    per agent."""
    return {
        agent: {
            "windows": [
                {"key": "five_hour", "label": "5시간", "utilization": value},
                {"key": "seven_day", "label": "7일", "utilization": 0.0},
            ]
        }
        for agent, value in utilization.items()
    }


def test_lead_starts_on_the_cheapest_rung(ladder):
    decision = decide(None, role=LEAD, usage={})

    # Nothing measured yet means nothing spent yet.
    assert (decision.agent, decision.model) == ("claude", "fable")


def test_lead_moves_on_when_its_window_is_spent(ladder):
    decision = decide(None, role=LEAD, usage=_usage(claude=0.4))

    assert (decision.agent, decision.model) == ("codex", "gpt-6-astra")


def test_lead_falls_through_to_the_last_rung(ladder):
    decision = decide(None, role=LEAD, usage=_usage(claude=0.4, codex=0.8))

    # The last rung has no limit: it is where everything lands.
    assert (decision.agent, decision.model) == ("claude", "opus")
    assert decision.reason == "마지막 단계"


def test_workers_use_their_own_ladder(ladder):
    # The worker ladder keeps using Codex far longer than the lead's does.
    assert decide(None, role=WORKER, usage=_usage(codex=0.5)).model == "gpt-6-astra"
    assert decide(None, role=WORKER, usage=_usage(codex=0.95)).model == "opus"


def test_an_explicit_agent_still_gets_that_ladder_model(ladder):
    decision = decide("codex", role=LEAD, usage=_usage(claude=0.0))

    # The UI's manual override picks the CLI; the model still comes from the
    # rung, so an override does not pin a model nobody chose.
    assert (decision.agent, decision.model) == ("codex", "gpt-6-astra")
    assert decision.explicit is True


def test_unknown_agent_names_go_through_the_ladder(ladder):
    assert decide("gpt-9", role=LEAD, usage={}).agent == "claude"


def test_unknown_agent_in_the_ladder_fails_loudly(tmp_path):
    path = tmp_path / "routing.yaml"
    path.write_text("lead:\n  - agent: gpt-9\n", encoding="utf-8")

    with pytest.raises(ValueError):
        ModelLadder.from_yaml(path)


def test_edited_ladder_is_picked_up_without_a_restart(ladder, monkeypatch):
    import os

    assert decide(None, role=LEAD, usage={}).model == "fable"

    ladder.write_text("lead:\n  - agent: codex\n    model: gpt-6-astra\n", encoding="utf-8")
    os.utime(ladder, (0, 0))

    assert decide(None, role=LEAD, usage={}).model == "gpt-6-astra"


def test_reported_utilization_is_used_as_is():
    tracker = UsageTracker()
    tracker.record_reported(
        "claude", {"unifiedWindows": {"five_hour": {"utilization": 0.42}}}
    )

    window = tracker.snapshot()["claude"]["windows"][0]

    # The provider's own number beats any estimate ANA could make.
    assert window["utilization"] == 0.42
    assert window["source"] == "reported"


def test_estimated_windows_count_only_what_falls_inside_them():
    tracker = UsageTracker(five_hour_budget=1000, weekly_budget=10_000)
    now = 1_000_000.0
    tracker.record_tokens("codex", 400, now=now - FIVE_HOURS - 60)  # older than 5h
    tracker.record_tokens("codex", 300, now=now - 60)

    five, week = tracker.snapshot(now=now)["codex"]["windows"]

    assert five["tokens"] == 300
    assert five["utilization"] == pytest.approx(0.3)
    assert five["source"] == "estimated"
    assert week["tokens"] == 700


def test_samples_older_than_a_week_are_dropped():
    tracker = UsageTracker()
    now = 2_000_000.0
    tracker.record_tokens("codex", 100, now=now - SEVEN_DAYS - 10)
    tracker.record_tokens("codex", 50, now=now)

    assert tracker.snapshot(now=now)["codex"]["windows"][1]["tokens"] == 50


def test_utilization_is_capped_at_one():
    tracker = UsageTracker(five_hour_budget=100, weekly_budget=100)
    tracker.record_tokens("codex", 500)

    assert tracker.snapshot()["codex"]["windows"][0]["utilization"] == 1.0


@pytest.fixture
def client(db_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as test_client:
        yield test_client


def test_usage_endpoint_reports_windows(client):
    client.app.state.usage_tracker.record_tokens("codex", 1234)

    payload = client.get("/usage").json()["agents"]

    assert payload["codex"]["windows"][0]["tokens"] == 1234
    assert [w["label"] for w in payload["codex"]["windows"]] == ["5시간", "7일"]


def test_usage_endpoint_lists_every_agent_before_any_turn(client, tmp_path, monkeypatch):
    """Every CLI ANA can route to has a row from the start: a bar that shows
    only the agents with data makes "nothing here" and "nothing spent" look
    the same."""
    # CODEX_HOME points at an empty directory: /usage re-reads Codex's rollout
    # log on every call, so the machine's real one would otherwise make this
    # test depend on whether anyone has run Codex here.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    client.app.state.usage_tracker._samples.clear()
    client.app.state.usage_tracker._reported.clear()

    agents = client.get("/usage").json()["agents"]

    assert set(agents) == {"claude", "codex", "kimi", "tapie"}
    # The second window is the plan's, not one shape for everyone: Kimi and
    # TAPIE are billed by the month, and TAPIE in dollars.
    assert [w["key"] for w in agents["claude"]["windows"]] == ["five_hour", "seven_day"]
    assert [w["key"] for w in agents["kimi"]["windows"]] == ["five_hour", "monthly"]
    assert [w["key"] for w in agents["tapie"]["windows"]] == ["five_hour", "monthly"]
    assert [w["unit"] for w in agents["tapie"]["windows"]] == ["usd", "usd"]
    assert [w["budget"] for w in agents["tapie"]["windows"]] == [50.0, 1500.0]
    for windows in agents.values():
        # Nothing spent and nothing reported: the bar says "estimated 0%",
        # not "unknown".
        assert all(w["source"] == "estimated" for w in windows["windows"])


def test_settings_round_trip(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    before = client.get("/settings").json()
    assert "lead:" in before["routing"]

    response = client.put(
        "/settings",
        json={
            "routing": "lead:\n  - agent: codex\n    model: gpt-6-astra\n",
            "system_prompt": "답변은 한국어로.",
        },
    )

    assert response.status_code == 200
    after = client.get("/settings").json()
    assert "codex" in after["routing"]
    assert after["system_prompt"] == "답변은 한국어로."
    assert decide(None, role=LEAD, usage={}).agent == "codex"


def test_legacy_routing_file_is_backed_up_and_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("ANA_ROUTING_RULES", raising=False)
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    old = "default_agent: claude\nsticky: true\nrules: []\n"
    (tmp_path / "routing.yaml").write_text(old, encoding="utf-8")

    from packages.orchestrator.settings import read_routing

    assert "lead:" in read_routing()
    assert (tmp_path / "routing.legacy.yaml").read_text(encoding="utf-8") == old
    assert decide(None, role=LEAD, usage={}).model == "fable"


def test_manual_usage_estimate_affects_routing_and_survives_restart(ladder, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    tracker = UsageTracker(five_hour_budget=1000)
    tracker.set_estimate("claude", "five_hour", 0.4)
    tracker.record_tokens("claude", 100)

    five = tracker.snapshot()["claude"]["windows"][0]
    assert five["source"] == "manual"
    assert five["utilization"] == pytest.approx(0.5)
    assert decide(None, role=LEAD, usage=tracker.snapshot()).agent == "codex"
    assert UsageTracker(five_hour_budget=1000).snapshot()["claude"]["windows"][0]["utilization"] == pytest.approx(0.4)


def test_settings_reject_unparseable_rules(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    response = client.put("/settings", json={"routing": "rules: [oops"})

    # The editor is exactly where a typo happens; a broken file must not reach
    # the router.
    assert response.status_code == 422
    assert "routing rules" in response.json()["detail"]


def test_settings_reject_rules_naming_an_unknown_agent(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    response = client.put(
        "/settings", json={"routing": "lead:\n  - agent: gpt-9\n    model: x\n"}
    )

    assert response.status_code == 422


def test_system_prompt_reaches_both_clis(tmp_path, monkeypatch):
    from packages.adapters.claude_code import ClaudeCodeAdapter
    from packages.adapters.codex import CodexAdapter

    claude_args, _ = ClaudeCodeAdapter(
        workspace_dir=str(tmp_path), env={}, system_prompt="한국어로"
    )._build_args("hi", None)
    codex_args = CodexAdapter(
        workspace_dir=str(tmp_path), env={}, system_prompt="한국어로"
    )._build_args("hi", None)

    # Claude has a flag for it; Codex has none for a one-shot run, so the same
    # words ride in front of the prompt.
    assert claude_args[claude_args.index("--append-system-prompt") + 1] == "한국어로"
    assert "한국어로" in codex_args[-1] and codex_args[-1].endswith("hi")


EFFORT_LADDER = """
lead:
  - agent: claude
    model: fable
    effort: low
    until: {window: five_hour, utilization: 0.25}
  - agent: codex
    model: gpt-6-astra
    effort: high
    until: {window: five_hour, utilization: 0.25}
  - agent: claude
    model: opus
    effort: xhigh
workers:
  default:
    - agent: codex
      model: gpt-6-astra
      effort: medium
"""


@pytest.fixture
def effort_ladder(tmp_path, monkeypatch):
    path = tmp_path / "routing.yaml"
    path.write_text(EFFORT_LADDER, encoding="utf-8")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(path))
    return path


def test_each_rung_carries_its_own_effort(effort_ladder):
    assert decide(None, role=LEAD, usage={}).effort == "low"
    assert decide(None, role=LEAD, usage=_usage(claude=0.5)).effort == "high"
    assert decide(None, role=LEAD, usage=_usage(claude=0.5, codex=0.9)).effort == "xhigh"
    assert decide(None, role=WORKER, usage={}).effort == "medium"


def test_a_rung_without_an_effort_leaves_it_to_the_cli(ladder):
    assert decide(None, role=LEAD, usage={}).effort is None


def test_settings_for_pairs_model_and_effort(effort_ladder):
    from packages.orchestrator.routing import settings_for

    assert settings_for("codex", role=LEAD) == ("gpt-6-astra", "high")
    assert settings_for("claude", role=LEAD) == ("fable", "low")


@pytest.mark.parametrize(
    "agent,effort",
    [
        ("claude", "minimal"),  # codex's vocabulary, not claude's
        # Verified against the provider's own 400: "'minimal' is not supported
        # with the 'gpt-6-sol' model".
        ("codex", "minimal"),
        ("claude", "hard"),
    ],
)
def test_an_effort_the_cli_does_not_have_fails_loudly(tmp_path, agent, effort):
    """Caught at parse time so the settings editor rejects it, rather than the
    CLI aborting the turn on an unknown flag value."""
    path = tmp_path / "routing.yaml"
    path.write_text(f"lead:\n  - agent: {agent}\n    effort: {effort}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="effort"):
        ModelLadder.from_yaml(path)


def test_claude_adapter_passes_the_effort_flag():
    from packages.adapters.claude_code import ClaudeCodeAdapter

    args, _ = ClaudeCodeAdapter(
        workspace_dir="/w", env={}, model="opus", effort="xhigh"
    )._build_args("hi", None)

    assert args[args.index("--effort") + 1] == "xhigh"


def test_codex_adapter_passes_the_effort_as_a_config_override():
    """Codex has no --effort flag; reasoning effort is a config value."""
    from packages.adapters.codex import CodexAdapter

    args = CodexAdapter(workspace_dir="/w", env={}, effort="high")._build_args("hi", None)

    assert 'model_reasoning_effort="high"' in args


@pytest.fixture
def estimates_client(db_pool, postgres_dsn, monkeypatch, tmp_path):
    """A client whose UsageTracker writes to a throwaway state directory.

    The tracker is constructed when the app starts, so the environment has to
    be set before TestClient enters — a monkeypatch inside the test body would
    land after it and the estimate would be written to the real ~/.ana, where
    it would then steer routing for every later test (and for the running
    server, if this is the same machine)."""
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_estimate_expires_at_the_reset_time(tmp_path, monkeypatch):
    """A window reset wipes the figure: after the reset the user's "40% spent"
    describes a window that no longer exists."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    tracker = UsageTracker(five_hour_budget=1000)
    resets_at = time.time() + 1800

    tracker.set_estimate("claude", "five_hour", 0.4, resets_at=resets_at)

    assert tracker.snapshot()["claude"]["windows"][0]["source"] == "manual"
    after = tracker.snapshot(now=resets_at + 1)["claude"]["windows"][0]
    assert after["source"] == "estimated"
    assert tracker.estimates() != {}


def test_estimate_without_a_reset_time_still_ages_out_after_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    tracker = UsageTracker(five_hour_budget=1000)

    tracker.set_estimate("claude", "five_hour", 0.4)

    assert tracker.snapshot()["claude"]["windows"][0]["source"] == "manual"
    later = tracker.snapshot(now=time.time() + FIVE_HOURS + 1)["claude"]["windows"][0]
    assert later["source"] == "estimated"


def test_estimate_survives_until_a_weekly_reset_date(tmp_path, monkeypatch):
    """The point of the date: a weekly figure entered today is still the truth
    five days from now, which the old entry+7d rule got right only by luck."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    tracker = UsageTracker(weekly_budget=1000)
    resets_at = time.time() + 5 * 24 * 60 * 60

    tracker.set_estimate("codex", "seven_day", 0.6, resets_at=resets_at)

    in_four_days = tracker.snapshot(now=time.time() + 4 * 24 * 60 * 60)["codex"]["windows"][1]
    assert in_four_days["source"] == "manual"
    assert tracker.estimates()["codex"]["seven_day"]["resets_at"] == pytest.approx(resets_at)


@pytest.mark.parametrize("offset", [-60, SEVEN_DAYS + 60])
def test_a_reset_time_that_cannot_be_this_window_is_refused(tmp_path, monkeypatch, offset):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    tracker = UsageTracker()

    with pytest.raises(ValueError):
        tracker.set_estimate("claude", "seven_day", 0.5, resets_at=time.time() + offset)


def test_estimate_endpoint_accepts_an_iso_reset_time(estimates_client):
    client = estimates_client
    resets_at = datetime.now(timezone.utc) + timedelta(hours=2)

    response = client.put(
        "/usage/estimates",
        json={
            "agent": "claude",
            "window": "five_hour",
            "utilization": 0.4,
            "resets_at": resets_at.isoformat(),
        },
    )

    assert response.status_code == 200
    stored = response.json()["estimates"]["claude"]["five_hour"]
    assert stored["utilization"] == pytest.approx(0.4)
    assert stored["resets_at"] == pytest.approx(resets_at.timestamp())


def test_estimate_endpoint_rejects_a_reset_time_in_the_past(estimates_client):
    client = estimates_client
    past = datetime.now(timezone.utc) - timedelta(minutes=1)

    response = client.put(
        "/usage/estimates",
        json={
            "agent": "claude",
            "window": "five_hour",
            "utilization": 0.4,
            "resets_at": past.isoformat(),
        },
    )

    assert response.status_code == 422


def test_auth_mode_is_an_editable_setting(client, tmp_path, monkeypatch):
    """Roadmap stage 1 deferred the pro/api switch to the stage 3 settings UI;
    until it existed, changing mode meant editing .env and restarting."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "api")

    assert client.get("/settings").json()["auth_mode"] == "api"

    saved = client.put("/settings", json={"auth_mode": "pro"})

    assert saved.status_code == 200
    assert saved.json()["auth_mode"] == "pro"
    # Stored in the state directory, so it outlives a restart — the env var is
    # only the default for a machine that has never set one.
    assert client.get("/settings").json()["auth_mode"] == "pro"


def test_an_unknown_auth_mode_is_refused(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    assert client.put("/settings", json={"auth_mode": "free"}).status_code == 422


def test_routing_reads_as_rungs_not_yaml(client, tmp_path, monkeypatch):
    """The settings form edits rungs. The vocabulary travels with them so it
    can offer exactly what the router accepts."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    body = client.get("/routing").json()

    assert body["lead"], "the lead ladder is never empty"
    first = body["lead"][0]
    assert set(first) == {"agent", "model", "effort", "window", "utilization"}
    assert "tapie" in body["agents"]
    assert set(body["efforts"]) >= {"claude", "codex", "kimi", "tapie"}
    assert [w["key"] for w in body["windows"]] == ["five_hour", "seven_day", "monthly"]
    # And which of them each CLI actually has, so a rung cannot point at a
    # window its plan does not run on.
    assert body["windows_by_agent"]["claude"] == ["five_hour", "seven_day"]
    assert body["windows_by_agent"]["tapie"] == ["five_hour", "monthly"]


def test_routing_round_trips_through_the_form(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    saved = client.put(
        "/routing",
        json={
            "lead": [
                {
                    "agent": "tapie",
                    "model": "opus",
                    "effort": "high",
                    "window": "five_hour",
                    "utilization": 0.4,
                },
                {"agent": "claude", "model": "fable", "effort": "", "window": "", "utilization": None},
            ],
            "workers": {"default": [{"agent": "codex", "model": "gpt-6-astra"}]},
        },
    )

    assert saved.status_code == 200
    assert saved.json()["lead"][0] == {
        "agent": "tapie",
        "model": "opus",
        "effort": "high",
        "window": "five_hour",
        "utilization": 0.4,
    }
    # The last rung keeps no threshold: it is where everything lands.
    assert saved.json()["lead"][1]["window"] == ""
    assert saved.json()["workers"]["default"][0]["agent"] == "codex"


def test_an_empty_ladder_is_refused(client, tmp_path, monkeypatch):
    """A ladder with no rungs would leave the router with nothing to choose,
    and the failure would land on the next turn rather than on the save."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    assert client.put("/routing", json={"lead": [], "workers": {}}).status_code == 400


def test_routing_carries_the_models_each_cli_takes(client, tmp_path, monkeypatch):
    """The editor offers names rather than asking someone to spell one. A model
    the rules already use is in the list even if this build has never heard of
    it, so editing a rung cannot quietly replace it."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    client.put(
        "/routing",
        json={
            "lead": [{"agent": "codex", "model": "gpt-6-experimental-unheard-of"}],
            "workers": {},
        },
    )

    models = client.get("/routing").json()["models"]

    assert "opus" in models["claude"]
    # TAPIE runs the same CLI, so the same names.
    assert models["tapie"] == models["claude"]
    assert models["codex"][0] == "gpt-6-experimental-unheard-of"
    assert "kimi" in models


def test_a_dollar_capped_window_measures_dollars():
    """TAPIE is a key against an endpoint with a spend cap: $50 rolling over
    five hours and $1500 a month. Counting its tokens against a token budget
    would show a number that means nothing."""
    from packages.observability.limits import UsageTracker

    tracker = UsageTracker()
    tracker.record_tokens("tapie", 40_000, usd=12.50)

    windows = {w["key"]: w for w in tracker.snapshot()["tapie"]["windows"]}

    assert windows["five_hour"]["utilization"] == 12.50 / 50.0
    assert windows["monthly"]["utilization"] == 12.50 / 1500.0
    assert windows["five_hour"]["usd"] == 12.5


def test_a_monthly_window_resets_on_the_first():
    """A monthly cap rolls over on the 1st, not a month after the last turn —
    and it says so without anybody having reported anything."""
    from datetime import datetime, timezone

    from packages.observability.limits import UsageTracker

    tracker = UsageTracker()
    now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc).timestamp()

    windows = {w["key"]: w for w in tracker.snapshot(now=now)["kimi"]["windows"]}
    resets = datetime.fromtimestamp(windows["monthly"]["resets_at"], tz=timezone.utc)

    assert (resets.year, resets.month, resets.day) == (2026, 10, 1)


def test_last_months_spending_is_not_in_this_months_window():
    from datetime import datetime, timezone

    from packages.observability.limits import UsageTracker

    tracker = UsageTracker()
    september = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc).timestamp()
    october = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc).timestamp()
    tracker.record_tokens("tapie", 0, usd=900.0, now=september)

    windows = {w["key"]: w for w in tracker.snapshot(now=october)["tapie"]["windows"]}

    assert windows["monthly"]["usd"] == 0.0
