"""Core queue worker logic."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HOME = Path.home()
DEFAULT_QUEUE_HOME = Path(os.environ.get("SAFE_AGENT_QUEUE_HOME", HOME / ".local" / "state" / "safe-agent-queue")).expanduser().resolve()

DIRS = ["pending", "running", "done", "failed", "logs", "workspaces"]

DENY_EXTRA_DIRS_DEFAULT = [
    HOME.resolve() / ".ssh",
    HOME.resolve() / ".gnupg",
    HOME.resolve() / ".config",
    HOME.resolve() / ".local" / "share" / "keyrings",
    HOME.resolve() / ".password-store",
]

DURATION_RE = re.compile(r"^(?P<num>\d+)(?P<unit>ms|s|m|h)$")

SAFETY_PREAMBLE = """You are a CLI agent running as a bounded background worker.
Rules:
1. Work only inside the provided task workspace unless the prompt explicitly mentions mounted extra directories.
2. Do not edit sensitive core system files, credentials, auth files, or personal accounts.
3. If a requested change touches sensitive/core systems, write a proposed plan and patch/diff instead of applying it.
4. Put the final concise result in RESULT.md in the task workspace.
5. Put any files you create under the task workspace.
6. If blocked, explain the blocker clearly in RESULT.md.
""".strip()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs(base: Path) -> None:
    for d in DIRS:
        (base / d).mkdir(parents=True, exist_ok=True)


def parse_duration_seconds(value: str) -> int:
    m = DURATION_RE.match(value.strip())
    if not m:
        raise ValueError("Duration must look like 30s, 10m, or 1h")
    n = int(m.group("num"))
    unit = m.group("unit")
    if unit == "ms":
        return max(1, n // 1000)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    raise ValueError(value)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip().lower()).strip("-._")
    return slug[:48] or "task"


def task_path(base: Path, queue: str, task_id: str) -> Path:
    return base / queue / f"{task_id}.json"


def load_task(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_task(base: Path, queue: str, task: dict[str, Any]) -> Path:
    p = task_path(base, queue, task["id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_extra_dirs(
    extra_dirs: list[str],
    deny_list: list[Path],
    allow_core: bool = False,
) -> list[Path]:
    resolved: list[Path] = []
    for raw in extra_dirs:
        p = Path(raw).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            raise ValueError(f"extra dir does not exist or is not a directory: {p}")
        if not allow_core:
            for denied in deny_list:
                if p == denied or is_relative_to(p, denied):
                    raise ValueError(
                        f"refusing to mount sensitive/core directory without --allow-core: {p}"
                    )
        resolved.append(p)
    return resolved


def build_prompt(task: dict[str, Any], workspace: Path, safety_preamble: str = SAFETY_PREAMBLE) -> str:
    task_file = workspace / "TASK.md"
    task_file.write_text(task["task"].strip() + "\n", encoding="utf-8")
    return (
        f"{safety_preamble}\n\n"
        f"Task ID: {task['id']}\n"
        f"Workspace: {workspace}\n"
        f"Task is written at: {task_file}\n\n"
        f"User task:\n{task['task'].strip()}\n"
    )


def prepare_command(
    cmd_template: str,
    prompt: str,
    workspace: Path,
    timeout: str,
    sandbox: bool,
    yolo: bool,
    extra_dirs: list[Path],
) -> list[str]:
    """Build the command from a template string with {placeholder} substitutions."""
    subs = {
        "prompt": prompt,
        "workspace": str(workspace),
        "timeout": timeout,
    }
    cmd_str = cmd_template.format(**subs)
    cmd = cmd_str.strip().split()
    return cmd


def run_task_file(
    base: Path,
    path: Path,
    cmd_template: str,
    deny_list: list[Path],
    safety_preamble: str = SAFETY_PREAMBLE,
    timeout_buffer: int = 90,
    quiet_success: bool = False,
) -> int:
    """Run a task from a given queue file path."""
    ensure_dirs(base)
    task = load_task(path)
    task_id = task["id"]
    running_path = task_path(base, "running", task_id)
    if path.parent != base / "running":
        path.replace(running_path)
    else:
        running_path = path

    workspace = Path(task["workspace"]).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    extra_dirs_resolved = validate_extra_dirs(
        task.get("extra_dirs", []),
        deny_list=deny_list,
        allow_core=bool(task.get("allow_core")),
    )

    task.update({"status": "running", "started_at": now_iso(), "updated_at": now_iso()})
    save_task(base, "running", task)

    prompt = build_prompt(task, workspace, safety_preamble=safety_preamble)
    log_file = base / "logs" / f"{task_id}.log"
    stdout_file = workspace / "stdout.txt"
    stderr_file = workspace / "stderr.txt"
    result_file = workspace / "RESULT.md"

    cmd = prepare_command(
        cmd_template,
        prompt=prompt,
        workspace=workspace,
        timeout=task.get("timeout", "20m"),
        sandbox=task.get("sandbox", True),
        yolo=task.get("yolo", True),
        extra_dirs=extra_dirs_resolved,
    )

    task["command"] = cmd
    task["log_file"] = str(log_file)
    task["stdout_file"] = str(stdout_file)
    task["stderr_file"] = str(stderr_file)
    save_task(base, "running", task)

    timeout_seconds = parse_duration_seconds(str(task.get("timeout", "20m"))) + timeout_buffer
    started = time.time()
    task_status = "failed"
    returncode = 1
    error_msg = None

    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            cwd=str(workspace),
            env=os.environ.copy(),
        )
        stdout_file.write_text(proc.stdout or "", encoding="utf-8")
        stderr_file.write_text(proc.stderr or "", encoding="utf-8")
        if not result_file.exists():
            result_file.write_text((proc.stdout or "").strip() + "\n", encoding="utf-8")
        task_status = "done" if proc.returncode == 0 else "failed"
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout_file.write_text(getattr(exc, "stdout", "") or "", encoding="utf-8")
        stderr_file.write_text(getattr(exc, "stderr", "") or "", encoding="utf-8")
        if not result_file.exists():
            result_file.write_text(f"Timed out after {timeout_seconds}s.\n", encoding="utf-8")
        task_status = "failed"
        returncode = 124
        error_msg = f"worker timeout after {timeout_seconds}s"
    except FileNotFoundError as exc:
        error_msg = f"command not found: {exc.filename}"
        task_status = "failed"
        returncode = 127

    task.update(
        {
            "status": task_status,
            "returncode": returncode,
            "error": error_msg,
            "finished_at": now_iso(),
            "updated_at": now_iso(),
            "duration_seconds": round(time.time() - started, 2),
            "result_file": str(result_file),
        }
    )

    final_path = save_task(base, task_status, task)
    try:
        running_path.unlink(missing_ok=True)
    except TypeError:
        if running_path.exists():
            running_path.unlink()
    summary = {"task_id": task_id, "status": task_status, "metadata": str(final_path), "result": str(result_file)}
    if not quiet_success or task_status != "done":
        print(json.dumps(summary, indent=2))
    return 0 if task_status == "done" else returncode


def oldest_pending(base: Path) -> Path | None:
    paths = sorted((base / "pending").glob("*.json"), key=lambda p: p.stat().st_mtime)
    return paths[0] if paths else None


def submit(
    base: Path,
    task_text: str,
    name: str | None = None,
    timeout: str = "20m",
    extra_dirs: list[str] | None = None,
    allow_core: bool = False,
    sandbox: bool = True,
    yolo: bool = True,
    deny_list: list[Path] | None = None,
    safety_preamble: str = SAFETY_PREAMBLE,
) -> tuple[str, Path]:
    """Submit a task to the pending queue."""
    ensure_dirs(base)
    if deny_list is None:
        deny_list = DENY_EXTRA_DIRS_DEFAULT
    validate_extra_dirs(extra_dirs or [], deny_list=deny_list, allow_core=allow_core)
    task_id = f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{slugify(name or task_text[:40])}-{uuid.uuid4().hex[:8]}"
    workspace = base / "workspaces" / task_id
    task = {
        "id": task_id,
        "name": name or task_text[:80],
        "status": "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "task": task_text.strip(),
        "timeout": timeout,
        "sandbox": sandbox,
        "yolo": yolo,
        "extra_dirs": [str(Path(p).expanduser().resolve()) for p in (extra_dirs or [])],
        "allow_core": allow_core,
        "workspace": str(workspace),
        "safety_preamble": safety_preamble,
    }
    save_task(base, "pending", task)
    return task_id, workspace


def list_tasks(base: Path, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
    """List all tasks across queues."""
    ensure_dirs(base)
    output: dict[str, list[dict[str, Any]]] = {}
    for q in ["pending", "running", "done", "failed"]:
        items: list[dict[str, Any]] = []
        for p in sorted((base / q).glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            t = load_task(p)
            items.append(
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "status": t.get("status"),
                    "created_at": t.get("created_at"),
                    "updated_at": t.get("updated_at"),
                    "workspace": t.get("workspace"),
                    "result_file": t.get("result_file"),
                }
            )
        output[q] = items
    return output


def show_task(base: Path, task_id: str) -> dict[str, Any] | None:
    """Show a specific task."""
    for q in ["pending", "running", "done", "failed"]:
        p = task_path(base, q, task_id)
        if p.exists():
            return load_task(p)
    return None