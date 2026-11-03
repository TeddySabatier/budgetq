"""Import tasks from JSON or Markdown checklists.

JSON: an array of objects with `prompt` (required) and optional `title`,
`executor`, `cwd`, `priority`.

Markdown: unchecked checklist items, one task per line:

    - [ ] @claude Fix flaky test :: Make tests/test_api.py pass reliably
    - [ ] @cursor(cwd=../web, priority=2) Add dark mode toggle
    - [ ] Write a CHANGELOG entry          (uses runner.default_executor)

Text after `::` is the prompt; without it the title is the prompt.
Checked items (`- [x]`) are ignored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

EXECUTORS = ("claude", "cursor")

LINE = re.compile(
    r"^\s*[-*]\s+\[ \]\s+"
    r"(?:@(?P<executor>\w+)(?:\((?P<opts>[^)]*)\))?\s+)?"
    r"(?P<body>.+?)\s*$"
)


@dataclass
class TaskSpec:
    title: str
    prompt: str
    executor: str
    cwd: str
    priority: int = 0


def _resolve_cwd(value: str | None, base: Path) -> str:
    if not value:
        return str(base)
    path = Path(value)
    return str(path if path.is_absolute() else (base / path).resolve())


def _check_executor(name: str) -> str:
    if name not in EXECUTORS:
        raise ValueError(f"unknown executor {name!r} (expected one of {', '.join(EXECUTORS)})")
    return name


def parse_markdown(text: str, base: Path, default_executor: str) -> list[TaskSpec]:
    specs = []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        opts = {}
        for part in (m["opts"] or "").split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                opts[key.strip()] = value.strip()
        body = m["body"]
        title, _, prompt = body.partition("::")
        title, prompt = title.strip(), prompt.strip() or title.strip()
        specs.append(
            TaskSpec(
                title=title,
                prompt=prompt,
                executor=_check_executor(m["executor"] or default_executor),
                cwd=_resolve_cwd(opts.get("cwd"), base),
                priority=int(opts.get("priority", 0)),
            )
        )
    return specs


def parse_json(text: str, base: Path, default_executor: str) -> list[TaskSpec]:
    items = json.loads(text)
    if isinstance(items, dict):
        items = items.get("tasks", [])
    specs = []
    for item in items:
        prompt = item["prompt"].strip()
        specs.append(
            TaskSpec(
                title=item.get("title") or prompt.splitlines()[0][:80],
                prompt=prompt,
                executor=_check_executor(item.get("executor") or default_executor),
                cwd=_resolve_cwd(item.get("cwd"), base),
                priority=int(item.get("priority", 0)),
            )
        )
    return specs


def parse_file(path: Path, default_executor: str) -> list[TaskSpec]:
    text = path.read_text(encoding="utf-8")
    base = path.parent.resolve()
    if path.suffix.lower() == ".json":
        return parse_json(text, base, default_executor)
    return parse_markdown(text, base, default_executor)
