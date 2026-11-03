from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

from trnje.workspace import inspect_file, replace_exact, run_checks


def test_inspect_file_is_bounded_and_reads_utf8(tmp_path):
    (tmp_path / "a.txt").write_text("héllo", encoding="utf-8")
    assert inspect_file(tmp_path, "a.txt").output == "héllo"
    assert inspect_file(tmp_path, "a.txt", max_bytes=2).status == "BLOCKED"


def test_replace_exact_only_changes_one_unique_match(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("alpha beta", encoding="utf-8")
    result = replace_exact(tmp_path, "a.txt", "beta", "gamma")
    assert result.status == "PASS"
    assert target.read_text(encoding="utf-8") == "alpha gamma"


def test_replace_exact_refuses_noop_replacement(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("unchanged", encoding="utf-8")

    result = replace_exact(tmp_path, "a.txt", "unchanged", "unchanged")

    assert result.status == "FAIL"
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_replace_exact_refuses_missing_or_duplicate_match(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("same same", encoding="utf-8")
    assert replace_exact(tmp_path, "a.txt", "absent", "new").status == "BLOCKED"
    assert replace_exact(tmp_path, "a.txt", "same", "new").status == "BLOCKED"
    assert target.read_text(encoding="utf-8") == "same same"


def test_workspace_escape_and_symlink_are_blocked(tmp_path):
    outside = tmp_path.parent / "outside-trnje-test.txt"
    outside.write_text("keep", encoding="utf-8")
    try:
        assert inspect_file(tmp_path, "../outside-trnje-test.txt").status == "BLOCKED"
        link = tmp_path / "linked.txt"
        link.symlink_to(outside)
        assert replace_exact(tmp_path, "linked.txt", "keep", "changed").status == "BLOCKED"
        assert outside.read_text(encoding="utf-8") == "keep"
    finally:
        outside.unlink(missing_ok=True)


def test_checks_use_fixed_commands_without_shell(tmp_path):
    with patch("trnje.workspace.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")) as run:
        result = run_checks(tmp_path)
    assert result.status == "PASS"
    assert run.call_count == 2
    assert all(call.kwargs["shell"] is False for call in run.call_args_list)


def test_check_failure_stops_following_commands(tmp_path):
    with patch("trnje.workspace.subprocess.run", return_value=CompletedProcess([], 1, "", "failed")) as run:
        result = run_checks(tmp_path)
    assert result.status == "FAIL"
    assert run.call_count == 1


def test_restore_if_unchanged_restores_original_content(tmp_path):
    from trnje.workspace import restore_if_unchanged

    target = tmp_path / "a.txt"
    target.write_text("bad edit\n", encoding="utf-8")

    result = restore_if_unchanged(
        tmp_path, "a.txt", expected_current="bad edit\n", original_content="original\n"
    )

    assert result.status == "PASS"
    assert target.read_text(encoding="utf-8") == "original\n"


def test_restore_if_unchanged_refuses_concurrent_change(tmp_path):
    from trnje.workspace import restore_if_unchanged

    target = tmp_path / "a.txt"
    target.write_text("concurrent edit\n", encoding="utf-8")

    result = restore_if_unchanged(
        tmp_path, "a.txt", expected_current="bad edit\n", original_content="original\n"
    )

    assert result.status == "BLOCKED"
    assert target.read_text(encoding="utf-8") == "concurrent edit\n"



def test_workspace_lock_is_released_after_exception(tmp_path):
    import pytest
    from trnje.workspace import workspace_lock

    with pytest.raises(RuntimeError):
        with workspace_lock(tmp_path):
            raise RuntimeError("simulated task failure")

    # A subsequent acquisition must not remain blocked by the failed task.
    with workspace_lock(tmp_path):
        pass


def test_workspace_lock_serializes_competing_threads(tmp_path):
    import threading
    from trnje.workspace import workspace_lock

    holder_entered = threading.Event()
    release_holder = threading.Event()
    contender_entered = threading.Event()

    def holder():
        with workspace_lock(tmp_path):
            holder_entered.set()
            assert release_holder.wait(2)

    def contender():
        with workspace_lock(tmp_path):
            contender_entered.set()

    first = threading.Thread(target=holder)
    first.start()
    assert holder_entered.wait(1)

    second = threading.Thread(target=contender)
    second.start()
    assert not contender_entered.wait(0.05)

    release_holder.set()
    assert contender_entered.wait(1)
    first.join(1)
    second.join(1)
    assert not first.is_alive()
    assert not second.is_alive()
