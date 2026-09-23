"""Reading Codex's real rate limits back out of its rollout log."""
import json
import os
import time

import pytest

from packages.observability.codex_usage import read_rate_limits
from packages.observability.limits import UsageTracker

# The shape codex-cli 0.156.1 writes (verified 2026-09-23), trimmed to what
# matters here: the record is the CLI's own event envelope, so the reader
# searches for `rate_limits` rather than addressing a fixed path.
def _record(primary: float, secondary: float, *, resets_in: int = 3600) -> str:
    now = time.time()
    return json.dumps(
        {
            "timestamp": "2026-09-23T07:54:20.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 13687}},
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": primary,
                        "window_minutes": 300,
                        "resets_at": now + resets_in,
                    },
                    "secondary": {
                        "used_percent": secondary,
                        "window_minutes": 10080,
                        "resets_at": now + resets_in * 24,
                    },
                },
            },
        }
    )


@pytest.fixture
def codex_home(tmp_path):
    directory = tmp_path / "codex_home" / "sessions" / "2026" / "09" / "23"
    directory.mkdir(parents=True)
    return tmp_path / "codex_home"


def _write(codex_home, name: str, lines: list[str], *, mtime: float | None = None):
    path = codex_home / "sessions" / "2026" / "09" / "23" / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_reads_the_windows_the_cli_recorded(codex_home):
    _write(codex_home, "rollout-a.jsonl", [_record(95.0, 29.0)])

    windows = read_rate_limits(codex_home)

    assert windows["five_hour"]["utilization"] == pytest.approx(0.95)
    assert windows["seven_day"]["utilization"] == pytest.approx(0.29)
    assert windows["five_hour"]["resets_at"] > time.time()


def test_the_last_record_in_a_file_wins(codex_home):
    _write(
        codex_home,
        "rollout-a.jsonl",
        ['{"type":"turn.started"}', _record(10.0, 5.0), _record(60.0, 11.0)],
    )

    windows = read_rate_limits(codex_home)

    assert windows["five_hour"]["utilization"] == pytest.approx(0.60)


def test_the_most_recently_written_session_wins(codex_home):
    now = time.time()
    _write(codex_home, "rollout-old.jsonl", [_record(90.0, 40.0)], mtime=now - 3600)
    _write(codex_home, "rollout-new.jsonl", [_record(12.0, 3.0)], mtime=now)

    assert read_rate_limits(codex_home)["five_hour"]["utilization"] == pytest.approx(0.12)


def test_a_file_without_limits_is_skipped_not_fatal(codex_home):
    _write(codex_home, "rollout-a.jsonl", ["not json at all", '{"type":"turn.started"}'])

    assert read_rate_limits(codex_home) is None


def test_no_codex_home_is_not_an_error(tmp_path):
    assert read_rate_limits(tmp_path / "nope") is None


def test_the_tracker_shows_codex_as_reported(codex_home, tmp_path, monkeypatch):
    """The point of the whole file: the Codex bar stops being ANA's estimate
    and starts being the provider's number."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    _write(codex_home, "rollout-a.jsonl", [_record(95.0, 29.0)])
    tracker = UsageTracker()

    tracker.record_reported_windows("codex", read_rate_limits(codex_home))

    five = tracker.snapshot()["codex"]["windows"][0]
    assert five["source"] == "reported"
    assert five["utilization"] == pytest.approx(0.95)


def test_a_reported_window_expires_at_its_reset(codex_home, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    _write(codex_home, "rollout-a.jsonl", [_record(95.0, 29.0, resets_in=600)])
    tracker = UsageTracker()
    tracker.record_reported_windows("codex", read_rate_limits(codex_home))

    after = tracker.snapshot(now=time.time() + 700)["codex"]["windows"][0]

    # Once the window has reset, "95% spent" describes a window that is gone.
    assert after["source"] == "estimated"


def test_claude_reset_times_are_kept_too(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    tracker = UsageTracker()
    resets_at = time.time() + 1800

    tracker.record_reported(
        "claude",
        {"unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": resets_at}}},
    )

    five = tracker.snapshot()["claude"]["windows"][0]
    assert five["source"] == "reported"
    assert five["resets_at"] == pytest.approx(resets_at)
    assert tracker.snapshot(now=resets_at + 1)["claude"]["windows"][0]["source"] == "estimated"


def test_reported_figures_survive_a_restart(tmp_path, monkeypatch):
    """They arrive only while a turn runs — the Claude CLI states them on its
    stream and nowhere else — so without persistence a restart left the bar
    reading "estimated 0%" until someone happened to run that CLI again."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    resets_at = time.time() + 3600
    tracker = UsageTracker()
    tracker.record_reported(
        "claude",
        {"unifiedWindows": {"five_hour": {"utilization": 0.42, "resetsAt": resets_at}}},
    )

    restarted = UsageTracker()

    five = restarted.snapshot()["claude"]["windows"][0]
    assert five["source"] == "reported"
    assert five["utilization"] == pytest.approx(0.42)


def test_a_stored_figure_past_its_reset_is_dropped_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    (tmp_path / "usage-reported.json").write_text(
        json.dumps(
            {"claude": {"five_hour": {"utilization": 0.9, "resets_at": time.time() - 1}}}
        ),
        encoding="utf-8",
    )

    five = UsageTracker().snapshot()["claude"]["windows"][0]

    # The window has reset; the number describes one that no longer exists.
    assert five["source"] == "estimated"
