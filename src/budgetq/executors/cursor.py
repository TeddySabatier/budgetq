"""Run a task with the Cursor CLI (`agent -p`).

The CLI reports no tokens or cost, so usage is measured by reading the
account's usage summary before and after the run (see quota/cursor.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from ..quota import QuotaState
from . import RunResult, drain, popen, start_timer

VERSION_DIR = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}(-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$")


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(p) for p in name.split("-")[0].split("."))


def detect_command() -> list[str] | None:
    """Find the Cursor CLI.

    On Windows the installer ships `agent.cmd`/`agent.ps1` wrappers around a bundled
    node.exe; calling node directly avoids cmd.exe mangling multi-line prompts.
    """
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "versions"
        if root.is_dir():
            versions = sorted(
                (d for d in root.iterdir() if d.is_dir() and VERSION_DIR.match(d.name)),
                key=lambda d: _version_key(d.name),
                reverse=True,
            )
            for d in versions:
                if (d / "node.exe").is_file() and (d / "index.js").is_file():
                    return [str(d / "node.exe"), str(d / "index.js")]
    for name in ("agent", "cursor-agent"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def build_command(cfg: dict, cwd: str) -> list[str] | None:
    base = list(cfg["command"]) or detect_command()
    if not base:
        return None
    cmd = base + ["-p", "--output-format", "json", "--trust", "--workspace", cwd]
    if cfg.get("force"):
        cmd.append("--force")
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    return cmd + list(cfg.get("extra_args", []))


def usage_delta(before: QuotaState | None, after: QuotaState | None) -> dict:
    """Percentage points of each pool (and on-demand cents) consumed between two reads."""
    if not before or not after or before.error or after.error:
        return {}
    if before.extra.get("cycle_start") != after.extra.get("cycle_start"):
        return {"note": "billing cycle reset during run"}
    delta = {}
    for name, window in after.windows.items():
        prev = before.windows.get(name)
        if prev and prev.utilization is not None and window.utilization is not None:
            delta[f"{name}_pct"] = round((window.utilization - prev.utilization) * 100, 3)
    b, a = before.extra.get("on_demand_used_cents"), after.extra.get("on_demand_used_cents")
    if a is not None and b is not None:
        delta["on_demand_cents"] = a - b
    return delta


def run(prompt: str, cwd: str, cfg: dict, output_path: Path, timeout: float) -> RunResult:
    cmd = build_command(cfg, cwd)
    if cmd is None:
        return RunResult(None, "not_found", "Cursor CLI not found (install it from cursor.com/cli)")
    env = {**os.environ, "CURSOR_INVOKED_AS": "agent"}
    timed_out = []
    proc = popen(cmd + [prompt], cwd, env)
    proc.stdin.close()
    timer = start_timer(proc, timeout, lambda: timed_out.append(True))

    with open(output_path.with_suffix(".err"), "w", encoding="utf-8") as err:
        err_thread = drain(proc.stderr, err)
        stdout = proc.stdout.read()
        proc.wait()
        err_thread.join(timeout=5)
    timer.cancel()
    output_path.write_text(stdout, encoding="utf-8")

    result = RunResult(exit_code=proc.returncode, outcome="error")
    payload = None
    for line in reversed(stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if payload:
        result.summary = str(payload.get("result", ""))[:500]
        result.tokens = {k: payload.get(k) for k in ("duration_ms", "session_id", "request_id") if payload.get(k)}
        failed = payload.get("is_error") or payload.get("subtype") not in (None, "success")
        result.outcome = "error" if failed or proc.returncode else "success"
    if timed_out:
        result.outcome, result.summary = "timeout", f"killed after {timeout:.0f}s"
    elif not payload:
        result.summary = output_path.with_suffix(".err").read_text(encoding="utf-8", errors="replace")[-500:] or stdout[-500:]
    return result
