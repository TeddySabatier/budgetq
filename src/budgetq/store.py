"""SQLite-backed task queue, run ledger and quota snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .quota import QuotaState

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    executor TEXT NOT NULL,
    cwd TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    executor TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    exit_code INTEGER,
    outcome TEXT,
    cost_usd_est REAL,
    tokens_json TEXT,
    quota_before_json TEXT,
    quota_after_json TEXT,
    summary TEXT,
    output_path TEXT
);
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executor TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap ON quota_snapshots(executor, fetched_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

STATUSES = ("queued", "running", "done", "failed", "skipped")


@dataclass
class Task:
    id: int
    title: str
    prompt: str
    executor: str
    cwd: str
    priority: int
    status: str
    attempts: int


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # tasks -------------------------------------------------------------
    def add_task(self, title: str, prompt: str, executor: str, cwd: str, priority: int = 0) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO tasks (title, prompt, executor, cwd, priority, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, prompt, executor, cwd, priority, now, now),
        )
        self.db.commit()
        return cur.lastrowid

    def get_task(self, task_id: int) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def list_tasks(self, status: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY id"
        return [_task(r) for r in self.db.execute(sql, args)]

    def next_task(self, executors: list[str]) -> Task | None:
        if not executors:
            return None
        marks = ",".join("?" * len(executors))
        row = self.db.execute(
            f"SELECT * FROM tasks WHERE status = 'queued' AND executor IN ({marks})"
            " ORDER BY priority DESC, id LIMIT 1",
            executors,
        ).fetchone()
        return _task(row) if row else None

    def queued_executors(self) -> set[str]:
        rows = self.db.execute("SELECT DISTINCT executor FROM tasks WHERE status = 'queued'")
        return {r["executor"] for r in rows}

    def set_status(self, task_id: int, status: str, bump_attempt: bool = False) -> None:
        self.db.execute(
            "UPDATE tasks SET status = ?, attempts = attempts + ?, updated_at = ? WHERE id = ?",
            (status, 1 if bump_attempt else 0, time.time(), task_id),
        )
        self.db.commit()

    def recover_running(self) -> int:
        """Tasks left 'running' by a crashed runner go back to the queue."""
        cur = self.db.execute("UPDATE tasks SET status = 'queued' WHERE status = 'running'")
        self.db.commit()
        return cur.rowcount

    # runs --------------------------------------------------------------
    def start_run(self, task: Task, quota_before: QuotaState | None, output_path: str) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (task_id, executor, started_at, quota_before_json, output_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (task.id, task.executor, time.time(), quota_before.to_json() if quota_before else None, output_path),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, exit_code, outcome, cost_usd_est, tokens, quota_after, summary) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, exit_code = ?, outcome = ?, cost_usd_est = ?,"
            " tokens_json = ?, quota_after_json = ?, summary = ? WHERE id = ?",
            (
                time.time(),
                exit_code,
                outcome,
                cost_usd_est,
                json.dumps(tokens) if tokens else None,
                quota_after.to_json() if quota_after else None,
                summary,
                run_id,
            ),
        )
        self.db.commit()

    def list_runs(self, limit: int = 20, task_id: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT runs.*, tasks.title FROM runs JOIN tasks ON tasks.id = runs.task_id"
        args: tuple = ()
        if task_id is not None:
            sql += " WHERE task_id = ?"
            args = (task_id,)
        sql += " ORDER BY runs.id DESC LIMIT ?"
        return list(self.db.execute(sql, (*args, limit)))

    def cost_since(self, executor: str, since: float) -> tuple[int, float]:
        row = self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd_est), 0) AS usd FROM runs"
            " WHERE executor = ? AND started_at >= ?",
            (executor, since),
        ).fetchone()
        return row["n"], row["usd"]

    # quota snapshots ---------------------------------------------------
    def save_snapshot(self, state: QuotaState) -> None:
        self.db.execute(
            "INSERT INTO quota_snapshots (executor, fetched_at, state_json) VALUES (?, ?, ?)",
            (state.executor, state.fetched_at, state.to_json()),
        )
        self.db.commit()

    def latest_snapshot(self, executor: str, source: str | None = None) -> QuotaState | None:
        for row in self.db.execute(
            "SELECT state_json FROM quota_snapshots WHERE executor = ? ORDER BY fetched_at DESC LIMIT 50",
            (executor,),
        ):
            state = QuotaState.from_json(row["state_json"])
            if state.error is None and (source is None or state.source == source):
                return state
        return None

    def snapshots_since(self, executor: str, since: float) -> list[QuotaState]:
        rows = self.db.execute(
            "SELECT state_json FROM quota_snapshots WHERE executor = ? AND fetched_at >= ? ORDER BY fetched_at",
            (executor, since),
        )
        return [s for s in (QuotaState.from_json(r["state_json"]) for r in rows) if s.error is None]

    # meta --------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        prompt=row["prompt"],
        executor=row["executor"],
        cwd=row["cwd"],
        priority=row["priority"],
        status=row["status"],
        attempts=row["attempts"],
    )
