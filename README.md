# safe-agent-queue

**Run CLI agents from a filesystem queue with isolated workspaces, scoped mounts, timeouts, logs, and auditable result files.**

> ⚠️ **Important caveat:** This tool manages a queue and workspace isolation at the application level. It is NOT an OS-level sandbox (no seccomp, namespaces, or mandatory access control). Do not conflate task queuing with secure sandboxing. A misconfigured `--add-dir`, `--allow-core`, or `--no-sandbox` can expose sensitive paths. Review task outputs before applying changes to important systems.

---

## Install

```bash
pip install safe-agent-queue
# or from source
pip install -e .
```

Verify:

```bash
safe-agent-queue --help
safe-agent-queue doctor
```

---

## Queue lifecycle

```
submit → pending → running → done
                         ↘ failed
```

- **pending/**: queued tasks waiting to run
- **running/**: tasks currently executing
- **done/**: tasks that completed with exit code 0
- **failed/**: tasks that exited non-zero or timed out
- **workspaces/**: per-task isolated working directories
- **logs/**: full agent log files per task

Each task file is JSON with full audit trail (status, timestamps, return code, stdout/stderr paths, result file path, command used).

---

## Quick start

### Submit and run immediately

```bash
safe-agent-queue run --task "Hello world"
```

### Submit only (for later processing)

```bash
safe-agent-queue submit --task "Background work"
```

### Run the oldest pending task

```bash
safe-agent-queue run-next
```

### List recent tasks

```bash
safe-agent-queue list --limit 20
```

### Show a task and its result

```bash
safe-agent-queue show <task-id>
```

---

## Command template

The `--cmd-template` flag controls how the agent is invoked. Use `{prompt}` as the placeholder for the generated prompt text.

**Default (echo only, useful for testing):**

```bash
safe-agent-queue run --task "say hello" --cmd-template "echo '{prompt}'"
```

**Generic CLI agent example (Codex/Claude-style):**

```bash
safe-agent-queue run \
  --task "Write a hello world program in Python" \
  --cmd-template "codex --print '{prompt}'"
```

**AGY (Antigravity CLI) style:**

```bash
# AGY with sandbox and skip-permissions (default behavior)
safe-agent-queue run \
  --task "List all files in the workspace" \
  --cmd-template "agy --print '{prompt}'"

# Disable sandbox
safe-agent-queue run --task "..." --no-sandbox \
  --cmd-template "agy --print '{prompt}'"
```

**Subprocess agent (any CLI that reads stdin or an argument):**

```bash
# If your agent reads from stdin
safe-agent-queue run --task "..." \
  --cmd-template "my-agent --input '{prompt}'"

# Workspace is available as {workspace}
safe-agent-queue run --task "..." \
  --cmd-template "my-agent --dir {workspace} --prompt '{prompt}'"
```

**Environment variable for persistent template:**

```bash
export SAFE_AGENT_CMD_TEMPLATE="my-agent --print '{prompt}'"
safe-agent-queue run --task "..."
```

---

## Workspace isolation

Each task gets its own workspace under `<queue-home>/workspaces/<task-id>/`. The agent command is run inside this directory. Extra directories can be mounted with `--add-dir`:

```bash
safe-agent-queue run \
  --task "Review logs" \
  --add-dir /var/log \
  --cmd-template "my-agent '{prompt}'"
```

---

## Timeout

```bash
safe-agent-queue run --task "long running task" --timeout 5m
```

Formats: `30s`, `10m`, `1h`, `500ms`. Default: `20m`.

---

## Deny list for sensitive mounts

By default, mounting of sensitive core directories is blocked. The default deny list includes:

- `$HOME/.ssh`
- `$HOME/.gnupg`
- `$HOME/.config`
- `$HOME/.local/share/keyrings`
- `$HOME/.password-store`
- `$HOME` itself (unless `--allow-core`)

```bash
# This will be refused unless you use --allow-core
safe-agent-queue run --task "..." --add-dir $HOME/.ssh
```

Use `--allow-core` only with explicit approval, and understand the risks.

Use `--no-default-deny` to disable the default deny list entirely.

---

## Safety preamble

Each task is wrapped with a safety preamble that instructs the agent to work only inside the workspace and not touch sensitive systems. You can customize it with `--safety-preamble`:

```bash
safe-agent-queue run \
  --task "..." \
  --safety-preamble "You are a helpful coding assistant. Work in {workspace} only."
```

---

## Configuration

### Queue home

Default: `$SAFE_AGENT_QUEUE_HOME` or `~/.local/state/safe-agent-queue`

```bash
safe-agent-queue list --queue-home /opt/my-queue
SAFE_AGENT_QUEUE_HOME=/opt/my-queue safe-agent-queue list
```

### Command template

```bash
export SAFE_AGENT_CMD_TEMPLATE="my-agent --print '{prompt}'"
safe-agent-queue run --task "..."
```

---

## Running as a service / cron

### systemd example

```ini
[Unit]
Description=Safe Agent Queue Worker
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -m safe_agent_queue run-next --silent
# or if installed as a script:
# ExecStart=/usr/bin/safe-agent-queue run-next --silent
User=youruser
Environment=SAFE_AGENT_QUEUE_HOME=/home/youruser/.local/state/safe-agent-queue
```

### cron example

```bash
# Run every 5 minutes if there is pending work
*/5 * * * * /usr/bin/safe-agent-queue run-next --silent --queue-home /home/youruser/.local/state/safe-agent-queue

# Log output to a file
*/5 * * * * /usr/bin/safe-agent-queue run-next --silent >> /home/youruser/queue-worker.log 2>&1
```

### Watchdog wrapper (like agy_queue_watchdog.sh)

```bash
#!/usr/bin/env bash
set -o pipefail
WORKER="safe-agent-queue"
TMP_OUT=$(mktemp)
TMP_ERR=$(mktemp)
cleanup() { rm -f "$TMP_OUT" "$TMP_ERR"; }
trap cleanup EXIT

"$WORKER" run-next --silent >"$TMP_OUT" 2>"$TMP_ERR"
rc=$?

if [[ $rc -ne 0 ]]; then
  echo "QUEUE_ALERT: worker returned rc=$rc"
  sed -n '1,120p' "$TMP_OUT"
  sed -n '1,120p' "$TMP_ERR"
  exit 0
fi

if [[ -s "$TMP_OUT" ]]; then
  sed -n '1,160p' "$TMP_OUT"
fi
```

---

## Result files

On completion, `RESULT.md` is written to the task workspace. It contains the agent's final output. If the agent didn't produce one, `stdout.txt` and `stderr.txt` capture the raw streams.

---

## Security disclaimer

**This is not an OS-level sandbox.** It provides:

- ✅ Filesystem queue with per-task workspaces
- ✅ Scope-limited extra mounts with a deny list
- ✅ Timeout enforcement
- ✅ stdout/stderr/log capture
- ✅ Auditable JSON task files

It does NOT provide:

- ❌ seccomp or syscall filtering
- ❌ Linux namespaces (mount, PID, network, user)
- ❌ Mandatory access control (AppArmor, SELinux)
- ❌ Cgroups resource limits
- ❌ Process isolation beyond `cwd=` and environment copying

Do not rely on this tool to contain a malicious or broken agent. Treat it as a workflow manager with best-effort workspace scoping, not a security boundary.

---

## License

MIT