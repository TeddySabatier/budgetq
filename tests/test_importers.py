from pathlib import Path

import pytest

from budgetq.importers import parse_json, parse_markdown

BASE = Path("/work").resolve()


def test_markdown_checklist():
    text = """
# Backlog
- [ ] @claude Fix flaky test :: Make tests/test_api.py pass reliably
- [x] @claude Already done
- [ ] @cursor(cwd=web, priority=2) Add dark mode toggle
- [ ] Write a CHANGELOG entry
not a task
"""
    specs = parse_markdown(text, BASE, "claude")
    assert [s.title for s in specs] == ["Fix flaky test", "Add dark mode toggle", "Write a CHANGELOG entry"]
    assert specs[0].prompt == "Make tests/test_api.py pass reliably"
    assert specs[1].executor == "cursor" and specs[1].priority == 2
    assert specs[1].cwd == str((BASE / "web").resolve())
    assert specs[2].executor == "claude" and specs[2].prompt == "Write a CHANGELOG entry"


def test_markdown_rejects_unknown_executor():
    with pytest.raises(ValueError):
        parse_markdown("- [ ] @gemini do it", BASE, "claude")


def test_json_array_and_object():
    specs = parse_json('[{"prompt": "Do A\\nmore", "executor": "cursor"}]', BASE, "claude")
    assert specs[0].title == "Do A" and specs[0].executor == "cursor" and specs[0].cwd == str(BASE)
    specs = parse_json('{"tasks": [{"prompt": "B", "title": "Bee", "priority": 3}]}', BASE, "claude")
    assert specs[0].title == "Bee" and specs[0].priority == 3
