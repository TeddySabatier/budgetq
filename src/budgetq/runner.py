"""The run loop: refresh quotas, pick a task whose executor is under budget, run it, log it."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .executors import RunResult
from .executors import claude as claude_exec
from .executors import cursor as cursor_exec
from .gate import DECIDERS, Decision
from .quota import QuotaState
from .quota import claude as claude_quota
from .quota import cursor as cursor_quota
from .store import Store, Task


@dataclass
class Quotas:
    """Reads and caches each executor's quota. Network readers are injectable for tests."""

    cfg: Config
    store: Store
    claude_probe: Callable[[], QuotaState] = claude_quota.probe
    cursor_fetch: Callable[[], QuotaState] = cursor_quota.fetch
    _last_probe: float = 0.0
    _probe_backoff_until: float = 0.0
    current: dict[str, QuotaState | None] = field(default_factory=dict)

    def refresh(self, executor: str, force: bool = False) -> QuotaState | None:
        if executor == "claude":
            state = self._refresh_claude(force)
        else:
            state = self.cursor_fetch()
            self.store.save_snapshot(state)
        self.current[executor] = state
        return state

    def _refresh_claude(self, force: bool) -> QuotaState | None:
        cfg = self.cfg.section("claude")
        now = time.time()
        due = now - self._last_probe >= cfg["probe_min_interval_seconds"]
        if cfg["probe_oauth_usage"] and now >= self._probe_backoff_until and (due or force):
            self._last_probe = now
            state = self.claude_probe()
            self.store.save_snapshot(state)
            if state.error:
                self._probe_backoff_until = now + max(state.extra.get("retry_after", 0), 900)
        return self._latest_claude()

    def _latest_claude(self) -> QuotaState | None:
        """Newest good reading, whichever source (probe or stream events) produced it."""
        candidates = [self.store.latest_snapshot("claude", s) for s in ("oauth", "stream")]
        candidates = [c for c in candidates if c]
        return max(candidates, key=lambda c: c.fetched_at) if candidates else None

    def record(self, state: QuotaState | None) -> None:
        if state is not None:
            self.store.save_snapshot(state)
            self.current[state.executor] = state


def decide(cfg: Config, executor: str, state: QuotaState | None) -> Decision:
    return DECIDERS[executor](state, cfg.section(executor))


def claude_abort_check(cfg: Config) -> Callable[[QuotaState], str | None]:
    limit = cfg.section("claude")["abort_mid_run_at"]

    def check(state: QuotaState) -> str | None:
        if state.status == "rejected":
            return "Claude rejected the request (limit reached)"
        for name, window in state.windows.items():
            if window.utilization is not None and window.utilization >= limit:
                return f"{name} at {window.utilization:.0%} (abort_mid_run_at {limit:.0%})"
        return None

    return check


def execute(cfg: Config, quotas: Quotas, task: Task, output_path) -> tuple[RunResult, QuotaState | None]:
    """Run one task; returns the result and the quota reading taken before it."""
    timeout = cfg.section("runner")["task_timeout_seconds"]
    before = quotas.current.get(task.executor)
    if task.executor == "claude":
        result = claude_exec.run(
            task.prompt, task.cwd, cfg.section("claude"), output_path, timeout, before, claude_abort_check(cfg)
        )
        if result.quota_after is not None and result.quota_after is not before:
            quotas.record(result.quota_after)
    else:
        result = cursor_exec.run(task.prompt, task.cwd, cfg.section("cursor"), output_path, timeout)
        after = quotas.refresh("cursor")
        result.quota_after = after
        result.tokens.update(cursor_exec.usage_delta(before, after))
    return result, before


def run_loop(
    cfg: Config,
    store: Store,
    quotas: Quotas,
    *,
    once: bool = False,
    max_tasks: int | None = None,
    dry_run: bool = False,
    only: list[str] | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    execute_fn: Callable = execute,
) -> int:
    """Drain the queue. Returns the number of tasks run."""
    recovered = store.recover_running()
    if recovered:
        log(f"requeued {recovered} task(s) left running by a previous runner")
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    enabled = [e for e in ("claude", "cursor") if cfg.section(e)["enabled"] and (not only or e in only)]
    ran = 0

    while True:
        if store.get_meta("paused"):
            log("runner is paused (`budgetq resume` to continue)")
            return ran
        queued = store.queued_executors() & set(enabled)
        if not queued:
            log("backlog empty")
            return ran

        decisions = {e: decide(cfg, e, quotas.refresh(e)) for e in sorted(queued)}
        open_execs = [e for e, d in decisions.items() if d.open]
        task = store.next_task(open_execs)

        if task is None:
            for e, d in decisions.items():
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.until)) if d.until else "next poll"
                log(f"{e}: paused, {d.reason} (until {when})")
            if once:
                return ran
            poll = cfg.section("runner")["poll_seconds"]
            wake = min([d.until for d in decisions.values() if d.until] + [time.time() + poll])
            sleep(max(30.0, min(wake - time.time() + 5, poll)))
            continue

        log(f"task #{task.id} [{task.executor}] {task.title}  ({decisions[task.executor].reason})")
        if dry_run:
            return ran

        store.set_status(task.id, "running", bump_attempt=True)
        output_path = cfg.runs_dir / f"task-{task.id}-attempt-{task.attempts + 1}.jsonl"
        run_id = store.start_run(task, quotas.current.get(task.executor), str(output_path))
        try:
            result, _ = execute_fn(cfg, quotas, task, output_path)
        except BaseException:
            store.set_status(task.id, "queued")
            store.finish_run(run_id, exit_code=None, outcome="interrupted", cost_usd_est=None,
                             tokens=None, quota_after=None, summary="runner interrupted")
            raise
        store.finish_run(
            run_id,
            exit_code=result.exit_code,
            outcome=result.outcome,
            cost_usd_est=result.cost_usd_est,
            tokens=result.tokens,
            quota_after=result.quota_after,
            summary=result.summary,
        )
        if result.outcome in ("not_found", "auth_error"):
            store.set_status(task.id, "queued")
            log(f"  {result.summary}")
            if result.outcome == "auth_error":
                log("  log in again (run `claude`, then /login) and re-run budgetq; the task is still queued")
            return ran
        store.set_status(task.id, "queued" if result.requeue else ("done" if result.outcome == "success" else "failed"))
        cost = f" ~${result.cost_usd_est:.4f}" if result.cost_usd_est is not None else ""
        usage = {k: v for k, v in result.tokens.items() if k.endswith("_pct") or k == "on_demand_cents"}
        log(f"  -> {result.outcome}{cost} {usage or ''}".rstrip())
        ran += 1
        if once or (max_tasks and ran >= max_tasks):
            return ran
