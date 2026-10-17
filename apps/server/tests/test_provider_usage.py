"""Account-quota readers for Claude / Cursor / Kimi (no live network)."""
import pytest

from packages.observability.claude_usage import normalize_oauth_usage
from packages.observability.cursor_usage import normalize_period_usage
from packages.observability.kimi_usage import normalize_usages


def test_claude_oauth_usage_divides_percent_and_parses_iso():
    windows = normalize_oauth_usage(
        {
            "five_hour": {"utilization": 17.0, "resets_at": "2026-10-17T08:00:00+00:00"},
            "seven_day": {"utilization": 74.0, "resets_at": "2026-10-18T02:00:00+00:00"},
        }
    )
    assert windows["five_hour"]["utilization"] == 0.17
    assert windows["seven_day"]["utilization"] == 0.74
    assert windows["five_hour"]["resets_at"] == 1792224000.0


def test_cursor_period_usage_maps_to_monthly():
    windows = normalize_period_usage(
        {
            "billingCycleEnd": "1792809123879",
            "planUsage": {"totalPercentUsed": 2.99},
        }
    )
    assert windows is not None
    assert windows["monthly"]["utilization"] == pytest.approx(0.0299)
    assert windows["monthly"]["resets_at"] == pytest.approx(1792809123.879)


def test_kimi_usages_already_ratio():
    windows = normalize_usages(
        {
            "usages": {
                "limit_5h": {"used_ratio": 0.0, "reset_time": "2026-10-17T06:31:37Z"},
                "limit_month_total": {
                    "used_ratio": 0.8082,
                    "reset_time": "2026-10-24T00:00:00Z",
                },
            }
        }
    )
    assert windows["five_hour"]["utilization"] == 0.0
    assert windows["monthly"]["utilization"] == 0.8082
    assert "resets_at" in windows["monthly"]

