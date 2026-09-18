import pytest

from packages.adapters.env_builder import (
    EnvBuilder,
    default_effort_from_settings,
    default_mode_from_settings,
    default_model_from_settings,
)


def test_pro_mode_strips_api_env_vars():
    base_env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "leftover",
        "ANTHROPIC_AUTH_TOKEN": "leftover",
        "ANTHROPIC_BASE_URL": "https://panel.tapie.kr/api/claude-code",
    }
    env = EnvBuilder("pro").build(base_env)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["PATH"] == "/usr/bin"


def test_api_mode_injects_billing_env_vars():
    base_env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY_SAVED": "test-key-value"}
    env = EnvBuilder("api").build(base_env)
    assert env["ANTHROPIC_API_KEY"] == "test-key-value"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-key-value"
    assert env["ANTHROPIC_BASE_URL"] == "https://panel.tapie.kr/api/claude-code"


def test_api_mode_without_saved_key_raises():
    with pytest.raises(RuntimeError):
        EnvBuilder("api").build({"PATH": "/usr/bin"})


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        EnvBuilder("bogus")


def test_default_mode_from_settings_reads_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "pro")
    assert default_mode_from_settings() == "pro"


def test_default_mode_from_settings_falls_back(monkeypatch):
    monkeypatch.delenv("CLAUDE_AUTH_MODE", raising=False)
    assert default_mode_from_settings(default="api") == "api"


def test_default_model_from_settings_reads_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "opus")
    assert default_model_from_settings() == "opus"


def test_default_model_from_settings_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert default_model_from_settings() is None


def test_default_effort_from_settings_reads_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    assert default_effort_from_settings() == "high"


def test_default_effort_from_settings_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_EFFORT", raising=False)
    assert default_effort_from_settings() is None


def test_database_url_is_stripped_from_pro_mode_env():
    """Finding #3: the auto-approved agent subprocess (--dangerously-skip-
    permissions) has no legitimate need for direct DB access; leaving
    DATABASE_URL in its environment would hand it working credentials to
    ANA's own memory database."""
    base_env = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgresql://postgres:secret@postgres:5432/ana",
    }
    env = EnvBuilder("pro").build(base_env)
    assert "DATABASE_URL" not in env


def test_database_url_is_stripped_from_api_mode_env():
    base_env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY_SAVED": "test-key-value",
        "DATABASE_URL": "postgresql://postgres:secret@postgres:5432/ana",
    }
    env = EnvBuilder("api").build(base_env)
    assert "DATABASE_URL" not in env
