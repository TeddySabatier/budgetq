"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from . import config as config_mod
from .gate import DECIDERS
from .importers import EXECUTORS, parse_file
from .quota import QuotaState
from .runner import Quotas, run_loop
from .store import STATUSES, Store


def _fmt_time(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch).strftime("%a %d %b %H:%M")


def _age(epoch: float) -> str:
    secs = max(0, time.time() - epoch)
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f}m ago"
    return f"{secs / 3600:.1f}h ago"


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "." * (width - filled)


def _week_start() -> float:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=today.weekday())).timestamp()


def _open(args) -> tuple[config_mod.Config, Store]:
    home = config_mod.find_home()
    if home is None:
        sys.exit("No budgetq.toml found here or in a parent folder. Run `budgetq init` first.")
    cfg = config_mod.load(home)
    return cfg, Store(cfg.db_path)


# commands ---------------------------------------------------------------
def cmd_init(args) -> None:
    home = Path(args.dir).resolve()
    home.mkdir(parents=True, exist_ok=True)
    path = home / config_mod.CONFIG_NAME
    if path.exists():
        print(f"{path} already exists; leaving it unchanged.")
    else:
        probe = args.probe
        if probe is None and sys.stdin.isatty():
            print(
                "Read the full Claude quota (weekly, per-model, extra usage) from Anthropic's\n"
                "undocumented OAuth usage endpoint, using the token Claude Code already stored?\n"
                "Without it, budgetq only sees the quota Claude Code reports while a task runs."
            )
            probe = input("Enable the probe? [y/N] ").strip().lower() in ("y", "yes")
        config_mod.write_default(home, bool(probe))
        print(f"wrote {path}")
    Store(home / config_mod.DATA_DIR / "budgetq.db").close()
    gitignore = home / ".gitignore"
    if (home / ".git").exists() and ".budgetq" not in (gitignore.read_text() if gitignore.exists() else ""):
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write("\n.budgetq/\n")
    print("next: budgetq add \"<prompt>\"   or   budgetq import backlog.md   then   budgetq run")


def cmd_add(args) -> None:
    cfg, store = _open(args)
    executor = args.executor or cfg.section("runner")["default_executor"]
    cwd = str(Path(args.cwd).resolve()) if args.cwd else str(Path.cwd())
    title = args.title or args.prompt.strip().splitlines()[0][:80]
    task_id = store.add_task(title, args.prompt, executor, cwd, args.priority)
    print(f"queued #{task_id} [{executor}] {title}")


def cmd_import(args) -> None:
    cfg, store = _open(args)
    specs = parse_file(Path(args.file), cfg.section("runner")["default_executor"])
    for s in specs:
        store.add_task(s.title, s.prompt, s.executor, s.cwd, s.priority)
    print(f"queued {len(specs)} task(s) from {args.file}")


def cmd_list(args) -> None:
    _, store = _open(args)
    tasks = store.list_tasks(args.status)
    if not tasks:
        print("no tasks")
    for t in tasks:
        print(f"#{t.id:<4} {t.status:<8} {t.executor:<7} p{t.priority:<2} x{t.attempts}  {t.title}")


def _print_state(name: str, state: QuotaState | None, cfg: dict) -> None:
    if state is None:
        print("  no data yet (appears after the first task runs, or enable probe_oauth_usage)")
        return
    if state.error:
        print(f"  error: {state.error}")
        return
    scale = 1.0 if name == "claude" else 100.0
    for wname, w in sorted(state.windows.items()):
        limit = cfg["thresholds"].get(wname)
        lim = "" if limit is None else (f"  limit {limit:.0%}" if scale == 1.0 else f"  limit {limit:g}%")
        if w.utilization is None:
            print(f"  {wname:<17} {'?':>5}{'':22}resets {_fmt_time(w.resets_at)}{lim}")
            continue
        print(f"  {wname:<17} {w.utilization:>5.0%} [{_bar(w.utilization)}] resets {_fmt_time(w.resets_at)}{lim}")
    extra = state.extra
    if name == "claude" and extra.get("extra_usage_enabled"):
        print(f"  extra usage       {extra.get('extra_usage_used')} / {extra.get('extra_usage_limit')} (monthly)")
    if name == "cursor":
        plan = extra.get("membership") or "?"
        cycle = f"{_fmt_time(extra.get('cycle_start'))} -> {_fmt_time(extra.get('cycle_end'))}"
        print(f"  plan {plan}, cycle {cycle}")
        if extra.get("on_demand_used_cents") is not None:
            limit = extra.get("on_demand_limit_cents")
            limit_txt = f" / ${limit / 100:.2f}" if limit else ""
            enabled = "on" if extra.get("on_demand_enabled") else "off"
            print(f"  on-demand ({enabled})   ${extra['on_demand_used_cents'] / 100:.2f}{limit_txt}")
    print(f"  source: {state.source}, read {_age(state.fetched_at)}")


def claude_login_state(command: list[str]) -> str | None:
    exe = shutil.which(command[0])
    if exe is None:
        return f"`{command[0]}` not found on PATH"
    try:
        proc = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        info = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not info.get("loggedIn"):
        return "LOGGED OUT - run `claude` and /login before budgetq can run Claude tasks"
    plan = info.get("subscriptionType") or info.get("authMethod") or "?"
    return f"logged in ({plan})"


def _cursor_week(store: Store) -> str | None:
    snaps = [s for s in store.snapshots_since("cursor", _week_start()) if "total" in s.windows]
    if len(snaps) < 2:
        return None
    used, prev = 0.0, snaps[0]
    for s in snaps[1:]:
        a, b = prev.windows["total"].utilization, s.windows["total"].utilization
        if a is not None and b is not None:
            used += b - a if s.extra.get("cycle_start") == prev.extra.get("cycle_start") else b
        prev = s
    return f"+{used * 100:.1f} pts of the cycle pool since Monday (from {len(snaps)} readings)"


def cmd_status(args) -> None:
    cfg, store = _open(args)
    quotas = Quotas(cfg, store)
    if store.get_meta("paused"):
        print("RUNNER PAUSED (budgetq resume)\n")
    for name in ("claude", "cursor"):
        section = cfg.section(name)
        if not section["enabled"]:
            print(f"{name}: disabled\n")
            continue
        state = quotas.refresh(name, force=True)
        decision = DECIDERS[name](state, section)
        verdict = "OPEN" if decision.open else "PAUSED"
        print(f"{name.upper()}  [{verdict}] {decision.reason}")
        if name == "claude":
            login = claude_login_state(section["command"])
            if login:
                print(f"  Claude Code login: {login}")
        _print_state(name, state, section)
        n_week, usd_week = store.cost_since(name, _week_start())
        line = f"  budgetq runs since Monday: {n_week}"
        if name == "claude":
            line += f", ~${usd_week:.2f} at API prices"
        print(line)
        if name == "cursor":
            week = _cursor_week(store)
            if week:
                print(f"  this week: {week}")
        print()
    counts = {s: len(store.list_tasks(s)) for s in STATUSES}
    print("queue: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v) if any(counts.values()) else "queue: empty")


def cmd_run(args) -> None:
    cfg, store = _open(args)
    if args.resume:
        store.set_meta("paused", None)
    quotas = Quotas(cfg, store)
    try:
        n = run_loop(
            cfg,
            store,
            quotas,
            once=args.once,
            max_tasks=args.max_tasks,
            dry_run=args.dry_run,
            only=[args.only] if args.only else None,
        )
    except KeyboardInterrupt:
        print("\nstopped; the interrupted task was put back in the queue")
        return
    print(f"ran {n} task(s)")


def cmd_log(args) -> None:
    _, store = _open(args)
    for r in store.list_runs(args.limit, args.task):
        took = f"{(r['finished_at'] - r['started_at']) / 60:.1f}m" if r["finished_at"] else "running"
        cost = f"${r['cost_usd_est']:.4f}" if r["cost_usd_est"] is not None else "-"
        print(f"run {r['id']:<4} task #{r['task_id']:<4} {r['executor']:<7} {r['outcome'] or '-':<14} {cost:>9} {took:>7}  {r['title']}")
        if args.verbose:
            if r["tokens_json"]:
                print(f"         usage: {r['tokens_json']}")
            if r["summary"]:
                print(f"         {r['summary'][:300]}")
            print(f"         output: {r['output_path']}")


def cmd_retry(args) -> None:
    _, store = _open(args)
    for task_id in args.ids:
        if store.get_task(task_id) is None:
            print(f"#{task_id}: no such task")
            continue
        store.set_status(task_id, "queued")
        print(f"#{task_id} queued")


def cmd_skip(args) -> None:
    _, store = _open(args)
    for task_id in args.ids:
        store.set_status(task_id, "skipped")
        print(f"#{task_id} skipped")


def cmd_pause(args) -> None:
    _, store = _open(args)
    store.set_meta("paused", str(time.time()))
    print("paused: runners stop before their next task")


def cmd_resume(args) -> None:
    _, store = _open(args)
    store.set_meta("paused", None)
    print("resumed")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="budgetq", description=__doc__ or "budget-gated backlog runner")
    p.add_argument("--version", action="version", version=f"budgetq {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create budgetq.toml and the queue in a folder")
    s.add_argument("dir", nargs="?", default=".")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--probe", dest="probe", action="store_true", default=None, help="enable the Claude OAuth usage probe")
    g.add_argument("--no-probe", dest="probe", action="store_false")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add", help="queue one task")
    s.add_argument("prompt")
    s.add_argument("-e", "--executor", choices=EXECUTORS)
    s.add_argument("-t", "--title")
    s.add_argument("-C", "--cwd", help="folder the agent works in (default: current folder)")
    s.add_argument("-p", "--priority", type=int, default=0)
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("import", help="queue tasks from a .md checklist or .json file")
    s.add_argument("file")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("list", help="list tasks")
    s.add_argument("-s", "--status", choices=STATUSES)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("status", help="show quota, gate decisions and queue")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("run", help="drain the queue until it is empty (waits while over budget)")
    s.add_argument("--once", action="store_true", help="run at most one task, never wait")
    s.add_argument("-n", "--max-tasks", type=int)
    s.add_argument("--only", choices=EXECUTORS)
    s.add_argument("--dry-run", action="store_true", help="show the next task and gate decision, run nothing")
    s.add_argument("--resume", action="store_true", help="clear a `budgetq pause` first")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("log", help="show recent runs with cost and usage")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--task", type=int)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_log)

    for name, func, text in (("retry", cmd_retry, "requeue tasks"), ("skip", cmd_skip, "skip tasks")):
        s = sub.add_parser(name, help=text)
        s.add_argument("ids", nargs="+", type=int)
        s.set_defaults(func=func)

    sub.add_parser("pause", help="stop runners before their next task").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="undo pause").set_defaults(func=cmd_resume)
    return p


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
