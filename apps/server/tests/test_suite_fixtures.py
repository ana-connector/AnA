"""The suite's own housekeeping: the age check that decides what gets swept.

A run killed by a short `timeout` or by the OOM killer never reaches the
database fixture's cleanup, so leaked containers are also collected by age at
the start of the next run. That makes the age arithmetic load-bearing in one
direction: reading a timestamp as older than it is would remove the database
of a pytest running in another terminal. These cases are the ones that got
that wrong -- a zone offset dropped while trimming Docker's nanoseconds, and a
timestamp that cannot be parsed at all.
"""
from datetime import datetime, timedelta, timezone

from .conftest import STALE_CONTAINER_SECONDS, _age_seconds


def _stamp(moment: datetime, fraction: str = ".123456789", zone: str = "Z") -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + fraction + zone


def test_a_container_started_now_is_not_stale():
    age = _age_seconds(_stamp(datetime.now(timezone.utc)))
    assert age < 5
    assert age < STALE_CONTAINER_SECONDS


def test_an_hour_old_container_is_stale():
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    age = _age_seconds(_stamp(started, fraction=".000000000"))
    assert 3590 < age < 3610
    assert age > STALE_CONTAINER_SECONDS


def test_a_zone_offset_survives_the_nanosecond_trim():
    """The trap: dropping "+09:00" makes a container that started a minute ago
    look nine hours old, and that is the one mistake that deletes a running
    test's database."""
    here = timezone(timedelta(hours=9))
    started = datetime.now(timezone.utc).astimezone(here)
    assert _age_seconds(_stamp(started, zone="+09:00")) < 5


def test_a_timestamp_without_a_fraction_still_reads():
    started = datetime.now(timezone.utc) - timedelta(minutes=45)
    assert _age_seconds(_stamp(started, fraction="")) > STALE_CONTAINER_SECONDS


def test_an_unreadable_timestamp_counts_as_too_young_to_sweep():
    # Docker's "never started" zero value, and a line that is not a timestamp.
    for unreadable in ("0001-01-01T00:00:00Z", "", "   ", "garbage", "2026-13-45T99:99:99Z"):
        assert _age_seconds(unreadable) == 0.0
