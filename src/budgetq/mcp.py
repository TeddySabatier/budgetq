"""MCP server (stdio) so any MCP client can add to and inspect the backlog.

Implements the small slice of the Model Context Protocol a tool server needs
(initialize, tools/list, tools/call, ping) over newline-delimited JSON-RPC on
stdin/stdout, with no third-party dependency. stdout carries protocol messages
only; anything else goes to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from .gate import DECIDERS
from .importers import EXECUTORS
from .runner import Quotas
from .store import STATUSES, Store

DEFAULT_PROTOCOL = "2025-06-18"

INSTRUCTIONS = (
    "budgetq is the user's background task queue: tasks added here are run later, unattended, "
    "by Claude Code or the Cursor CLI, only while the user's plan quota is under budget. "
    "Write each prompt so an agent with no access to this conversation can do the task alone: "
    "name the files, the expected result, and how to verify it. Always pass `cwd` as the "
    "absolute path of the project the task applies to."
)

TASK_FIELDS = {
    "prompt": {"type": "string", "description": "Complete, self-contained instructions for the agent that will run the task."},
    "title": {"type": "string", "description": "Short label (defaults to the first line of the prompt)."},
    "executor": {"type": "string", "enum": list(EXECUTORS), "description": "Which agent runs it (default from budgetq.toml)."},
    "cwd": {"type": "string", "description": "Absolute path of the folder the agent works in."},
    "priority": {"type": "integer", "description": "Higher runs first (default 0)."},
}

TOOLS = [
    {
        "name": "add_task",
        "description": "Queue one task for budgetq to run in the background when budget allows.",
        "inputSchema": {"type": "object", "properties": TASK_FIELDS, "required": ["prompt", "cwd"]},
    },
    {
        "name": "add_tasks",
        "description": "Queue several tasks at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "object", "properties": TASK_FIELDS, "required": ["prompt", "cwd"]},
                }
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List queued (or other) tasks in the backlog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(STATUSES), "description": "Filter (default: queued)."},
                "limit": {"type": "integer", "description": "Max tasks returned (default 50)."},
            },
        },
    },
    {
        "name": "budget_status",
        "description": "Current Claude and Cursor quota usage, whether each executor may run, and queue counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "recent_runs",
        "description": "Recent task runs with outcome, estimated cost and a result summary.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Default 10."}}},
    },
    {
        "name": "update_task",
        "description": "Skip a task (remove it from the queue) or requeue a failed/skipped one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "action": {"type": "string", "enum": ["skip", "retry"]},
            },
            "required": ["id", "action"],
        },
    },
]


class ToolError(Exception):
    pass


class Server:
    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)

    # tools ---------------------------------------------------------------
    def _check(self, spec: dict) -> tuple[str, str, str, str, int]:
        prompt = (spec.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required")
        executor = spec.get("executor") or self.cfg.section("runner")["default_executor"]
        if executor not in EXECUTORS:
            raise ToolError(f"executor must be one of {', '.join(EXECUTORS)}")
        cwd = Path(spec.get("cwd") or self.cfg.home).expanduser()
        if not cwd.is_absolute():
            raise ToolError("cwd must be an absolute path")
        if not cwd.is_dir():
            raise ToolError(f"cwd does not exist: {cwd}")
        title = (spec.get("title") or prompt.splitlines()[0])[:80]
        return title, prompt, executor, str(cwd), int(spec.get("priority") or 0)

    def _insert(self, checked: tuple[str, str, str, str, int]) -> dict:
        title, prompt, executor, cwd, priority = checked
        task_id = self.store.add_task(title, prompt, executor, cwd, priority)
        return {"id": task_id, "title": title, "executor": executor, "cwd": cwd}

    def add_task(self, args: dict) -> dict:
        return {"queued": self._insert(self._check(args))}

    def add_tasks(self, args: dict) -> dict:
        checked = []
        for i, spec in enumerate(args.get("tasks") or []):  # validate all before queueing any
            try:
                checked.append(self._check(spec))
            except ToolError as exc:
                raise ToolError(f"task {i + 1}: {exc}") from None
        if not checked:
            raise ToolError("tasks is empty")
        return {"queued": [self._insert(c) for c in checked]}

    def list_tasks(self, args: dict) -> dict:
        tasks = self.store.list_tasks(args.get("status") or "queued")[: int(args.get("limit") or 50)]
        return {
            "tasks": [
                {"id": t.id, "title": t.title, "executor": t.executor, "status": t.status,
                 "priority": t.priority, "attempts": t.attempts, "cwd": t.cwd}
                for t in tasks
            ]
        }

    def budget_status(self, args: dict) -> dict:
        quotas = Quotas(self.cfg, self.store)
        out: dict = {"paused_by_user": bool(self.store.get_meta("paused"))}
        for name in EXECUTORS:
            section = self.cfg.section(name)
            if not section["enabled"]:
                out[name] = {"enabled": False}
                continue
            state = quotas.refresh(name)
            decision = DECIDERS[name](state, section)
            out[name] = {
                "may_run": decision.open,
                "reason": decision.reason,
                "until": decision.until,
                "usage": None if state is None else {
                    k: {"used_fraction": w.utilization, "resets_at": w.resets_at}
                    for k, w in state.windows.items()
                },
                "source": None if state is None else state.source,
                "error": None if state is None else state.error,
            }
        out["queue"] = {s: len(self.store.list_tasks(s)) for s in STATUSES}
        return out

    def recent_runs(self, args: dict) -> dict:
        rows = self.store.list_runs(int(args.get("limit") or 10))
        return {
            "runs": [
                {"run": r["id"], "task": r["task_id"], "title": r["title"], "executor": r["executor"],
                 "outcome": r["outcome"], "cost_usd_est": r["cost_usd_est"], "summary": r["summary"]}
                for r in rows
            ]
        }

    def update_task(self, args: dict) -> dict:
        task = self.store.get_task(int(args["id"]))
        if task is None:
            raise ToolError(f"no task #{args['id']}")
        if task.status == "running":
            raise ToolError("task is running; wait for it to finish")
        status = {"skip": "skipped", "retry": "queued"}.get(args.get("action"))
        if status is None:
            raise ToolError("action must be skip or retry")
        self.store.set_status(task.id, status)
        return {"id": task.id, "status": status}

    # protocol --------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            return None  # notification (e.g. notifications/initialized)
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
            return _result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "budgetq", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
            if fn is None:
                return _error(msg_id, -32602, f"unknown tool {name}")
            try:
                payload, is_error = fn(args), False
            except (ToolError, KeyError, ValueError, TypeError) as exc:
                payload, is_error = {"error": str(exc)}, True
            return _result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(payload, indent=1)}],
                "isError": is_error,
            })
        return _error(msg_id, -32601, f"method not found: {method}")


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(cfg: config_mod.Config, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(cfg)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "batches not supported")
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
