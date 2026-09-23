from packages.orchestrator import handoff


def test_normalize_agent_accepts_known_agents():
    assert handoff.normalize_agent("claude") == "claude"
    assert handoff.normalize_agent("codex") == "codex"


def test_normalize_agent_falls_back_on_unknown_or_missing():
    # Client-supplied and used to select a subprocess, so anything unknown
    # must land on the default rather than propagate.
    assert handoff.normalize_agent(None) == handoff.DEFAULT_AGENT
    assert handoff.normalize_agent("") == handoff.DEFAULT_AGENT
    assert handoff.normalize_agent("../../bin/sh") == handoff.DEFAULT_AGENT


def test_build_handoff_prompt_includes_recent_turns_and_agents():
    prompt = handoff.build_handoff_prompt(
        "이어서 리팩터링 해줘",
        incoming_agent="codex",
        previous_agent="claude",
        turns=[
            {"prompt": "이 파일 구조 설명해줘", "response": "이건 어댑터 레이어야", "agent": "claude"},
        ],
    )

    assert "이어서 리팩터링 해줘" in prompt
    assert "이 파일 구조 설명해줘" in prompt
    assert "이건 어댑터 레이어야" in prompt
    assert "claude" in prompt and "codex" in prompt


def test_build_handoff_prompt_returns_prompt_unchanged_without_turns():
    prompt = handoff.build_handoff_prompt(
        "hello",
        incoming_agent="codex",
        previous_agent="claude",
        turns=[],
    )
    assert prompt == "hello"


def test_build_handoff_prompt_truncates_long_fields():
    long_response = "x" * 5000
    prompt = handoff.build_handoff_prompt(
        "continue",
        incoming_agent="codex",
        previous_agent="claude",
        turns=[{"prompt": "q", "response": long_response, "agent": "claude"}],
    )

    # The whole brief becomes a single argv element, capped by Linux at
    # MAX_ARG_STRLEN (131072 bytes); every field must be bounded.
    assert long_response not in prompt
    assert "..." in prompt
    assert len(prompt) < 2000


def test_handoff_keeps_the_end_of_a_long_recent_answer():
    prompt = handoff.build_handoff_prompt(
        "continue", incoming_agent="codex", previous_agent="claude",
        turns=[{"prompt": "q", "response": "a" * 1000 + "final decision", "agent": "claude"}],
    )

    assert "final decision" in prompt
