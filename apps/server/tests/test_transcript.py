from packages.adapters.transcript import TranscriptLogger


def test_append_and_read_all_round_trip(tmp_path):
    logger = TranscriptLogger(str(tmp_path))

    logger.append(session_id="s1", prompt="hi", response="hello", cost_usd=0.01)
    logger.append(session_id="s1", prompt="how are you", response="good", cost_usd=0.02)

    records = logger.read_all()
    assert len(records) == 2
    assert records[0]["prompt"] == "hi"
    assert records[0]["response"] == "hello"
    assert records[0]["cost_usd"] == 0.01
    assert records[1]["prompt"] == "how are you"
    assert all("timestamp" in r and r["session_id"] == "s1" for r in records)


def test_read_all_on_missing_file_returns_empty_list(tmp_path):
    logger = TranscriptLogger(str(tmp_path))
    assert logger.read_all() == []
