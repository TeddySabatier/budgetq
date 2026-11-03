import time

from budgetq.config import Config, DEFAULTS, _merge
from budgetq.executors import RunResult
from budgetq.quota import QuotaState, Window
from budgetq.runner import Quotas, run_loop
from budgetq.store import Store


def setup(tmp_path, overrides=None):
    cfg = Config(home=tmp_path, data=_merge(DEFAULTS, overrides or {}))
    store = Store(cfg.db_path)
    return cfg, store


def cursor_state(total):
    return QuotaState("cursor", "dashboard", time.time(), {"total": Window(total, time.time() + 86400)}, extra={"cycle_start": 1})


def fake_execute(outcomes):
    calls = []

    def execute(cfg, quotas, task, output_path):
        calls.append(task.id)
        return outcomes.pop(0), None

    return execute, calls


def test_runs_queue_in_priority_order(tmp_path):
    cfg, store = setup(tmp_path)
    a = store.add_task("a", "a", "claude", str(tmp_path))
    b = store.add_task("b", "b", "claude", str(tmp_path), priority=5)
    execute, calls = fake_execute([RunResult(0, "success", cost_usd_est=0.1), RunResult(1, "error")])
    n = run_loop(cfg, store, Quotas(cfg, store), log=lambda m: None, execute_fn=execute)
    assert n == 2 and calls == [b, a]
    assert store.get_task(b).status == "done" and store.get_task(a).status == "failed"
    assert store.cost_since("claude", 0) == (2, 0.1)


def test_over_budget_executor_is_skipped_others_run(tmp_path):
    cfg, store = setup(tmp_path)
    c = store.add_task("c", "c", "cursor", str(tmp_path), priority=9)
    k = store.add_task("k", "k", "claude", str(tmp_path))
    quotas = Quotas(cfg, store, cursor_fetch=lambda: cursor_state(0.85))
    execute, calls = fake_execute([RunResult(0, "success")])
    logs = []
    run_loop(cfg, store, quotas, once=True, log=logs.append, execute_fn=execute)
    assert calls == [k]
    assert store.get_task(c).status == "queued"


def test_once_reports_pause_and_runs_nothing(tmp_path):
    cfg, store = setup(tmp_path)
    store.add_task("c", "c", "cursor", str(tmp_path))
    quotas = Quotas(cfg, store, cursor_fetch=lambda: cursor_state(0.9))
    execute, calls = fake_execute([])
    logs = []
    assert run_loop(cfg, store, quotas, once=True, log=logs.append, execute_fn=execute) == 0
    assert calls == [] and any("paused" in m for m in logs)


def test_waits_then_resumes_when_quota_drops(tmp_path):
    cfg, store = setup(tmp_path)
    store.add_task("c", "c", "cursor", str(tmp_path))
    readings = [cursor_state(0.9), cursor_state(0.5)]
    quotas = Quotas(cfg, store, cursor_fetch=lambda: readings.pop(0) if len(readings) > 1 else readings[0])
    execute, calls = fake_execute([RunResult(0, "success")])
    sleeps = []
    run_loop(cfg, store, quotas, log=lambda m: None, sleep=sleeps.append, execute_fn=execute)
    assert len(sleeps) == 1 and len(calls) == 1


def test_quota_abort_requeues(tmp_path):
    cfg, store = setup(tmp_path)
    t = store.add_task("t", "t", "claude", str(tmp_path))
    execute, _ = fake_execute([RunResult(None, "aborted_quota", requeue=True)])
    run_loop(cfg, store, Quotas(cfg, store), once=True, log=lambda m: None, execute_fn=execute)
    assert store.get_task(t).status == "queued" and store.get_task(t).attempts == 1


def test_pause_flag_stops_runner(tmp_path):
    cfg, store = setup(tmp_path)
    store.add_task("t", "t", "claude", str(tmp_path))
    store.set_meta("paused", "1")
    execute, calls = fake_execute([])
    assert run_loop(cfg, store, Quotas(cfg, store), log=lambda m: None, execute_fn=execute) == 0 and calls == []


def test_claude_probe_used_when_enabled(tmp_path):
    cfg, store = setup(tmp_path, {"claude": {"probe_oauth_usage": True}})
    store.add_task("t", "t", "claude", str(tmp_path))
    probe = lambda: QuotaState("claude", "oauth", time.time(), {"seven_day": Window(0.85, time.time() + 3600)})
    execute, calls = fake_execute([])
    logs = []
    run_loop(cfg, store, Quotas(cfg, store, claude_probe=probe), once=True, log=logs.append, execute_fn=execute)
    assert calls == [] and any("seven_day" in m for m in logs)
