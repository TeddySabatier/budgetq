"""Run a task with Claude Code in headless mode (`claude -p`, stream-json)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from ..quota import QuotaState
from ..quota.claude import apply_rate_limit_event
from . import RunResult, drain, kill_tree, popen, start_timer


def build_command(cfg: dict) -> list[str]:
    cmd = list(cfg["command"])
    resolved = shutil.which(cmd[0])
    if resolved:
        cmd[0] = resolved
    cmd += ["-p", "--output-format", "stream-json", "--verbose", "--max-turns", str(cfg["max_turns"])]
    if cfg.get("max_budget_usd"):
        cmd += ["--max-budget-usd", str(cfg["max_budget_usd"])]
    if cfg.get("permission_mode"):
        cmd += ["--permission-mode", cfg["permission_mode"]]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    cmd += [arg for arg in cfg.get("extra_args", []) if isinstance(arg, str) and arg.strip()]
    if cfg.get("allowed_tools"):  # variadic flag: keep it last
        cmd += ["--allowedTools", *cfg["allowed_tools"]]
    return cmd


def classify(event: dict) -> tuple[str, str]:
    """Outcome and summary of a stream-json `result` event.

    `subtype` can say "success" while `is_error` is true (e.g. an expired login),
    so `is_error` wins.
    """
    subtype = event.get("subtype") or ""
    text = str(event.get("result") or ", ".join(event.get("errors") or []) or subtype)[:500]
    if not event.get("is_error") and subtype == "success":
        return "success", text
    if "authenticat" in text.lower() or "/login" in text:
        return "auth_error", text
    if subtype and subtype != "success":
        return subtype, text
    return event.get("terminal_reason") or "error", text


def run(
    prompt: str,
    cwd: str,
    cfg: dict,
    output_path: Path,
    timeout: float,
    quota: QuotaState | None,
    should_abort: Callable[[QuotaState], str | None],
) -> RunResult:
    """Run one task. `should_abort` sees every quota update; a non-None reason stops the run."""
    if not prompt or not isinstance(prompt, str):
        return RunResult(None, "error", "prompt must be a non-empty string")
    cmd = build_command(cfg)
    if shutil.which(cmd[0]) is None:
        return RunResult(None, "not_found", f"Claude Code not found: {cfg['command'][0]}")

    timed_out = []
    abort_reason = None
    result_event: dict | None = None
    proc = popen(cmd, cwd)
    proc.stdin.write(prompt)
    proc.stdin.close()
    timer = start_timer(proc, timeout, lambda: timed_out.append(True))

    with open(output_path, "w", encoding="utf-8") as out, open(output_path.with_suffix(".err"), "w", encoding="utf-8") as err:
        err_thread = drain(proc.stderr, err)
        for line in proc.stdout:
            out.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "rate_limit_event":
                quota = apply_rate_limit_event(event, quota)
                if abort_reason is None:
                    abort_reason = should_abort(quota)
                    if abort_reason:
                        kill_tree(proc)
            elif kind == "result":
                result_event = event
        proc.wait()
        err_thread.join(timeout=5)
    timer.cancel()

    result = RunResult(exit_code=proc.returncode, outcome="error", quota_after=quota)
    if result_event:
        result.cost_usd_est = result_event.get("total_cost_usd")
        usage = result_event.get("usage") or {}
        result.tokens = {
            k: usage.get(k)
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            if usage.get(k) is not None
        }
        result.tokens["num_turns"] = result_event.get("num_turns")
        result.outcome, result.summary = classify(result_event)
        result.requeue = result.outcome == "auth_error"

    if abort_reason:
        result.outcome, result.requeue = "aborted_quota", True
        result.summary = f"stopped mid-run: {abort_reason}"
    elif timed_out:
        result.outcome, result.summary = "timeout", f"killed after {timeout:.0f}s"
    elif not result_event:
        result.summary = output_path.with_suffix(".err").read_text(encoding="utf-8", errors="replace")[-500:]
    return result
