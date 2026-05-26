"""Tests for safe-agent-queue."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from safe_agent_queue import core


def test_parse_duration_seconds():
    assert core.parse_duration_seconds("30s") == 30
    assert core.parse_duration_seconds("10m") == 600
    assert core.parse_duration_seconds("1h") == 3600
    assert core.parse_duration_seconds("500ms") == 1
    assert core.parse_duration_seconds("1s") == 1
    with pytest.raises(ValueError):
        core.parse_duration_seconds("invalid")
    with pytest.raises(ValueError):
        core.parse_duration_seconds("")


def test_slugify():
    assert core.slugify("Hello World") == "hello-world"
    assert core.slugify("Test 123!") == "test-123"
    assert core.slugify("  spaces  ") == "spaces"
    assert core.slugify("") == "task"
    long_name = "a" * 100
    assert len(core.slugify(long_name)) <= 48


def test_submit_and_list(tmp_path):
    core.ensure_dirs(tmp_path)
    task_id, workspace = core.submit(tmp_path, "echo hello", name="test task", timeout="10s")
    assert task_id.startswith("20")
    assert workspace.exists() or str(workspace).startswith(str(tmp_path))
    result = core.list_tasks(tmp_path)
    assert "pending" in result
    pending = [t for t in result["pending"] if t["id"] == task_id]
    assert len(pending) == 1
    assert pending[0]["name"] == "test task"


def test_submit_validates_extra_dirs(tmp_path):
    core.ensure_dirs(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        core.submit(tmp_path, "echo hello", extra_dirs=["/nonexistent/path/abc123"])


def test_submit_resolves_and_checks_deny_list(tmp_path):
    core.ensure_dirs(tmp_path)
    deny_list = [tmp_path / "forbidden"]
    deny_list[0].mkdir()
    with pytest.raises(ValueError, match="refusing to mount"):
        core.submit(tmp_path, "echo hello", extra_dirs=[str(deny_list[0])], deny_list=deny_list)


def test_submit_allows_core_with_allow_core(tmp_path):
    core.ensure_dirs(tmp_path)
    deny_list = [tmp_path / "forbidden"]
    deny_list[0].mkdir()
    task_id, _ = core.submit(tmp_path, "echo hello", extra_dirs=[str(deny_list[0])], deny_list=deny_list, allow_core=True)
    assert task_id


def test_show_task(tmp_path):
    core.ensure_dirs(tmp_path)
    task_id, _ = core.submit(tmp_path, "show me", name="show test")
    task = core.show_task(tmp_path, task_id)
    assert task is not None
    assert task["id"] == task_id
    assert task["task"] == "show me"


def test_show_task_not_found(tmp_path):
    core.ensure_dirs(tmp_path)
    assert core.show_task(tmp_path, "nonexistent-id-12345") is None


def test_run_fake_command_success(tmp_path):
    core.ensure_dirs(tmp_path)
    task_id, _ = core.submit(tmp_path, "hello from queue", name="fake echo test", timeout="5s")
    path = core.task_path(tmp_path, "pending", task_id)

    def fake_cmd_template(prompt, workspace, timeout, sandbox, yolo, extra_dirs):
        return ["echo", prompt]

    # Run with a simple echo command
    result = core.run_task_file(
        tmp_path,
        path,
        cmd_template="echo '{prompt}'",
        deny_list=[],
    )
    assert result == 0
    done_tasks = core.list_tasks(tmp_path)["done"]
    done = [t for t in done_tasks if t["id"] == task_id]
    assert len(done) == 1
    assert done[0]["status"] == "done"


def test_run_timeout_failure(tmp_path):
    core.ensure_dirs(tmp_path)
    task_id, _ = core.submit(tmp_path, "sleep 100", name="timeout test", timeout="3s")
    path = core.task_path(tmp_path, "pending", task_id)
    result = core.run_task_file(
        tmp_path,
        path,
        cmd_template="sleep 100",
        deny_list=[],
    )
    assert result != 0
    failed = core.list_tasks(tmp_path)["failed"]
    assert any(t["id"] == task_id for t in failed)


def test_result_file_created(tmp_path):
    core.ensure_dirs(tmp_path)
    task_id, _ = core.submit(tmp_path, "write result", name="result test", timeout="5s")
    path = core.task_path(tmp_path, "pending", task_id)
    core.run_task_file(tmp_path, path, cmd_template="echo '{prompt}'", deny_list=[])
    task = core.show_task(tmp_path, task_id)
    result_file = Path(task["result_file"])
    assert result_file.exists()


def test_is_relative_to():
    parent = Path("/tmp/test")
    child = Path("/tmp/test/subdir/nested")
    assert core.is_relative_to(child, parent)
    sibling = Path("/tmp/other")
    assert not core.is_relative_to(sibling, parent)


def test_validate_extra_dirs(tmp_path):
    test_dir = tmp_path / "extra"
    test_dir.mkdir()
    resolved = core.validate_extra_dirs([str(test_dir)], deny_list=[])
    assert resolved[0] == test_dir.resolve()


def test_validate_extra_dirs_rejects_sensitive(tmp_path):
    denied = tmp_path / "denied"
    denied.mkdir()
    with pytest.raises(ValueError, match="refusing to mount"):
        core.validate_extra_dirs([str(denied)], deny_list=[denied])


def test_oldest_pending(tmp_path):
    core.ensure_dirs(tmp_path)
    id1, _ = core.submit(tmp_path, "first", name="task1")
    time.sleep(0.1)
    id2, _ = core.submit(tmp_path, "second", name="task2")
    oldest = core.oldest_pending(tmp_path)
    assert oldest is not None
    task = core.load_task(oldest)
    assert task["id"] == id1


def test_run_task_file_no_agent_bin(tmp_path):
    """Running with a missing binary should fail gracefully."""
    core.ensure_dirs(tmp_path)
    task_id, _ = core.submit(tmp_path, "test", name="missing bin test", timeout="2s")
    path = core.task_path(tmp_path, "pending", task_id)
    # Use a definitely nonexistent binary path
    result = core.run_task_file(
        tmp_path,
        path,
        cmd_template="/nonexistent/binary/path {prompt}",
        deny_list=[],
    )
    assert result != 0
    failed = core.list_tasks(tmp_path)["failed"]
    assert any(t["id"] == task_id for t in failed)


def test_task_lifecycle(tmp_path):
    """Full lifecycle: submit -> pending -> running -> done/failed."""
    core.ensure_dirs(tmp_path)
    task_id, _ = core.submit(tmp_path, "lifecycle test", name="lifecycle", timeout="5s")
    path = core.task_path(tmp_path, "pending", task_id)
    tasks = core.list_tasks(tmp_path)
    assert any(t["id"] == task_id for t in tasks["pending"])
    core.run_task_file(tmp_path, path, cmd_template="echo '{prompt}'", deny_list=[])
    tasks = core.list_tasks(tmp_path)
    assert any(t["id"] == task_id for t in tasks["done"])


def test_no_hermes_imports():
    """Verify no hermes imports in the codebase."""
    src = Path(__file__).parent.parent / "src"
    for py_file in src.rglob("*.py"):
        content = py_file.read_text()
        assert "hermes" not in content.lower(), f"Found hermes reference in {py_file}"
        assert "import hermes" not in content, f"Found hermes import in {py_file}"