"""CLI interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from safe_agent_queue import core

DEFAULT_TIMEOUT = "20m"
DEFAULT_CMD_TEMPLATE = "echo '{prompt}'"


def _resolve_base(args: argparse.Namespace) -> Path:
    return Path(args.queue_home or core.DEFAULT_QUEUE_HOME).expanduser().resolve()


def _deny_list_from_args(args: argparse.Namespace) -> list[Path]:
    if args.no_default_deny:
        return []
    return core.DENY_EXTRA_DIRS_DEFAULT


def cmd_submit(args: argparse.Namespace) -> int:
    base = _resolve_base(args)
    task_text = args.task or (Path(args.task_file).read_text(encoding="utf-8") if args.task_file else "")
    task_text = task_text.strip()
    if not task_text:
        print("No task text provided", file=sys.stderr)
        return 2
    deny_list = _deny_list_from_args(args)
    extra_dirs = args.add_dir or []
    if args.allow_core:
        deny_list = []
    task_id, workspace = core.submit(
        base=base,
        task_text=task_text,
        name=args.name,
        timeout=args.timeout,
        extra_dirs=extra_dirs,
        allow_core=args.allow_core,
        sandbox=not args.no_sandbox,
        yolo=not args.no_yolo,
        deny_list=deny_list,
        safety_preamble=args.safety_preamble or core.SAFETY_PREAMBLE,
    )
    print(json.dumps({"submitted": task_id, "workspace": str(workspace)}, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    base = _resolve_base(args)
    task_text = args.task or (Path(args.task_file).read_text(encoding="utf-8") if args.task_file else "")
    task_text = task_text.strip()
    if not task_text:
        print("No task text provided", file=sys.stderr)
        return 2
    deny_list = _deny_list_from_args(args)
    if args.allow_core:
        deny_list = []
    task_id, workspace = core.submit(
        base=base,
        task_text=task_text,
        name=args.name,
        timeout=args.timeout,
        extra_dirs=args.add_dir or [],
        allow_core=args.allow_core,
        sandbox=not args.no_sandbox,
        yolo=not args.no_yolo,
        deny_list=deny_list,
        safety_preamble=args.safety_preamble or core.SAFETY_PREAMBLE,
    )
    path = core.task_path(base, "pending", task_id)
    return core.run_task_file(
        base=base,
        path=path,
        cmd_template=args.cmd_template or DEFAULT_CMD_TEMPLATE,
        deny_list=deny_list,
        safety_preamble=args.safety_preamble or core.SAFETY_PREAMBLE,
    )


def cmd_run_next(args: argparse.Namespace) -> int:
    base = _resolve_base(args)
    core.ensure_dirs(base)
    p = core.oldest_pending(base)
    if not p:
        if not getattr(args, "silent", False):
            print(json.dumps({"status": "idle", "message": "no pending tasks"}, indent=2))
        return 0
    deny_list = _deny_list_from_args(args)
    return core.run_task_file(
        base=base,
        path=p,
        cmd_template=args.cmd_template or DEFAULT_CMD_TEMPLATE,
        deny_list=deny_list,
        safety_preamble=args.safety_preamble or core.SAFETY_PREAMBLE,
        quiet_success=bool(getattr(args, "silent", False)),
    )


def cmd_list(args: argparse.Namespace) -> int:
    base = _resolve_base(args)
    output = core.list_tasks(base, limit=args.limit)
    print(json.dumps(output, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    base = _resolve_base(args)
    task = core.show_task(base, args.task_id)
    if task is None:
        print(f"task not found: {args.task_id}", file=sys.stderr)
        return 1
    print(json.dumps(task, indent=2))
    result = Path(task.get("result_file") or Path(task.get("workspace", "")) / "RESULT.md")
    if result.exists():
        print("\n--- RESULT.md ---")
        print(result.read_text(encoding="utf-8", errors="replace"))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    base = _resolve_base(args)
    checks = []
    ok = True
    base_path = Path(args.queue_home or core.DEFAULT_QUEUE_HOME).expanduser().resolve()
    checks.append(("Queue home exists", base_path.exists()))
    if not base_path.exists():
        ok = False
    for d in core.DIRS:
        dp = base_path / d
        exists = dp.exists()
        checks.append((f"  directory ./{d}/", exists))
        if not exists:
            ok = False
    checks.append(("Python version", sys.version_info >= (3, 10)))
    if sys.version_info < (3, 10):
        ok = False
    print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Filesystem queue worker for CLI agents with workspace isolation, timeouts, and audit logs."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_submit = sub.add_parser("submit", help="Queue a task")
    p_run = sub.add_parser("run", help="Queue and immediately run a task")
    p_next = sub.add_parser("run-next", help="Run oldest pending task")
    p_list = sub.add_parser("list", help="List tasks")
    p_show = sub.add_parser("show", help="Show one task and its result")
    p_doctor = sub.add_parser("doctor", help="Check environment readiness")

    for p in [p_submit, p_run, p_next]:
        p.add_argument("--task", help="Task text")
        p.add_argument("--task-file", help="Read task text from file")
        p.add_argument("--name", help="Human-friendly task name")
        p.add_argument("--timeout", default=DEFAULT_TIMEOUT, help=f"Timeout, e.g. 30s, 10m, 1h (default: {DEFAULT_TIMEOUT})")
        p.add_argument("--add-dir", action="append", default=[], help="Extra directory to mount into agent workspace")
        p.add_argument("--allow-core", action="store_true", help="Allow mounting denied core/private dirs; use only with explicit approval")
        p.add_argument("--no-sandbox", action="store_true", help="Disable sandbox mode")
        p.add_argument("--no-yolo", action="store_true", help="Disable yolo/skip-permissions mode")
        p.add_argument("--no-default-deny", action="store_true", help="Do not use default deny list for --add-dir")
        p.add_argument("--safety-preamble", help="Custom safety preamble text")

    p_run.add_argument("--cmd-template", default=DEFAULT_CMD_TEMPLATE, help="Command template with {prompt} placeholder (default: echo '{prompt}')")

    p_next.add_argument("--silent", action="store_true", help="Suppress idle and successful-task stdout for no-agent cron use")
    p_run_next_shared = p_next
    p_run_next_shared.add_argument("--cmd-template", default=DEFAULT_CMD_TEMPLATE, help="Command template with {prompt} placeholder")

    for p in [p_submit, p_run, p_next, p_list, p_show, p_doctor]:
        p.add_argument("--queue-home", help=f"Queue root directory (default: $SAFE_AGENT_QUEUE_HOME or {core.DEFAULT_QUEUE_HOME})")

    p_list.add_argument("--limit", type=int, default=10)
    p_show.add_argument("task_id")

    p_submit.set_defaults(func=cmd_submit)
    p_run.set_defaults(func=cmd_run)
    p_next.set_defaults(func=cmd_run_next)
    p_list.set_defaults(func=cmd_list)
    p_show.set_defaults(func=cmd_show)
    p_doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"safe-agent-queue error: {exc}", file=sys.stderr)
        return 1