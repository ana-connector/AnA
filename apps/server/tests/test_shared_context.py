"""The context a lead hands every worker, and the workspace's memory of it."""
from pathlib import Path

from packages.orchestrator import context as shared_context
from packages.orchestrator.context import SharedContext


def test_a_plans_context_is_read_as_far_as_it_can_be_trusted():
    parsed = shared_context.parse(
        {"files": ["a.py", "a.py", 7, "  b.py  "], "notes": "  API note  ", "remember": 1}
    )

    assert parsed.files == ("a.py", "b.py")  # deduplicated, stripped, non-strings dropped
    assert parsed.notes == "API note"
    assert parsed.remember is True


def test_a_malformed_context_is_a_missing_convenience_not_a_broken_plan():
    assert shared_context.parse("files: a.py").empty
    assert shared_context.parse({"files": {"a": 1}, "notes": {"x": 1}}).empty
    # One path unwrapped rather than refused: the shape a model reaches for.
    assert shared_context.parse({"files": "a.py"}).files == ("a.py",)


def test_too_many_files_are_capped(tmp_path):
    parsed = shared_context.parse({"files": [f"f{i}.py" for i in range(40)]})

    assert len(parsed.files) == shared_context.MAX_FILES


def test_named_files_arrive_as_excerpts_so_workers_stop_re_reading_them(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "routing.py").write_text("def decide():\n    return 1\n", encoding="utf-8")

    block = shared_context.render(
        SharedContext(files=("pkg/routing.py",), notes="decide() returns a Rung."),
        str(tmp_path),
    )

    assert "[SHARED CONTEXT]" in block
    assert "decide() returns a Rung." in block
    assert "--- pkg/routing.py (lines 1-2 of 2) ---" in block
    assert "def decide():" in block


def test_a_long_file_is_truncated_on_a_line_boundary_and_says_so(tmp_path):
    lines = [f"line {i}\n" for i in range(4000)]
    (tmp_path / "big.py").write_text("".join(lines), encoding="utf-8")

    block = shared_context.render(SharedContext(files=("big.py",)), str(tmp_path))

    assert "truncated" in block
    assert f"of {len(lines)}" in block
    assert len(block) < shared_context.MAX_FILE_CHARS + 500
    # Never a half line: a worker reading "line 42" cut mid-token guesses.
    assert block.rstrip().splitlines()[-1] in [line.rstrip("\n") for line in lines]


def test_a_path_outside_the_workspace_is_named_not_read(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    block = shared_context.render(
        SharedContext(files=("../secret.txt", "/etc/hostname", "missing.py")), str(workspace)
    )

    assert "nope" not in block
    assert "not included" in block
    for named in ("../secret.txt", "/etc/hostname", "missing.py"):
        assert named in block


def test_no_context_renders_nothing_at_all(tmp_path):
    assert shared_context.render(None, str(tmp_path)) == ""
    assert shared_context.render(SharedContext(), str(tmp_path)) == ""
    assert shared_context.attach("do the thing", "") == "do the thing"


def test_notes_the_lead_marked_project_wide_survive_the_turn(tmp_path):
    context = SharedContext(files=("a.py",), notes="The gate owns confinement.", remember=True)

    assert shared_context.remember(str(tmp_path), context, title="게이트 조사") is True

    remembered = shared_context.recall(str(tmp_path))
    assert "The gate owns confinement." in remembered
    assert "게이트 조사" in remembered
    assert "관련 파일: a.py" in remembered
    # Handed to the next turn's workers even with no context of their own.
    assert "[PROJECT NOTES]" in shared_context.render(None, str(tmp_path), remembered=remembered)


def test_working_notes_about_one_request_are_not_remembered(tmp_path):
    assert shared_context.remember(str(tmp_path), SharedContext(notes="for this turn")) is False
    assert shared_context.recall(str(tmp_path)) == ""
    assert not (tmp_path / shared_context.CONTEXT_DIRNAME).exists()


def test_the_same_note_twice_is_not_two_facts(tmp_path):
    context = SharedContext(notes="one fact", remember=True)

    assert shared_context.remember(str(tmp_path), context) is True
    assert shared_context.remember(str(tmp_path), context) is False
    assert shared_context.recall(str(tmp_path)).count("one fact") == 1


def test_memory_drops_whole_oldest_entries_rather_than_half_a_note(tmp_path):
    for i in range(60):
        shared_context.remember(
            str(tmp_path), SharedContext(notes=f"fact {i} " + "x" * 400, remember=True),
            title=f"turn {i}",
        )

    remembered = shared_context.recall(str(tmp_path))
    assert len(remembered) <= shared_context.MAX_MEMORY_CHARS
    assert "fact 59" in remembered  # the newest note describes the repo as it is
    assert "fact 0" not in remembered
    # Every kept entry is a whole one, header included: an entry present at all
    # has its `## ` header and its full 400-character body.
    assert remembered.lstrip().startswith("## ")
    kept = [n for n in range(60) if f"fact {n} " in remembered]
    assert kept and kept == list(range(kept[0], 60))  # a contiguous tail, newest kept
    for number in kept:
        assert f"fact {number} " + "x" * 400 in remembered  # whole body, never a prefix
    assert remembered.count("## ") == len(kept)  # one header per kept entry


def test_a_read_only_workspace_does_not_fail_the_turn(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace.chmod(0o500)
    try:
        assert shared_context.remember(
            str(workspace), SharedContext(notes="x", remember=True)
        ) is False
        assert shared_context.recall(str(workspace)) == ""
    finally:
        workspace.chmod(0o700)


def test_the_context_goes_in_front_of_the_instruction():
    attached = shared_context.attach("do the thing", "[SHARED CONTEXT]\nstuff")

    assert attached.index("[SHARED CONTEXT]") < attached.index("[INSTRUCTION]")
    assert attached.endswith("do the thing")


def test_the_memory_directory_stays_out_of_the_users_git_status(tmp_path):
    shared_context.remember(str(tmp_path), SharedContext(notes="fact", remember=True))

    ignore = tmp_path / shared_context.CONTEXT_DIRNAME / ".gitignore"
    assert ignore.read_text(encoding="utf-8") == "*\n"
    # Nothing tracked in the workspace itself is touched.
    assert not (tmp_path / ".gitignore").exists()
