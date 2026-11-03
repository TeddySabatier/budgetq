import unittest
from pathlib import Path

from budgetq.importers import parse_json, parse_markdown

BASE = Path("/work").resolve()


class TestImportersMarkdown(unittest.TestCase):
    def test_markdown_checklist(self):
        text = """# Backlog
- [ ] @claude Fix flaky test :: Make tests/test_api.py pass reliably
- [x] @claude Already done
- [ ] @cursor(cwd=web, priority=2) Add dark mode toggle
- [ ] Write a CHANGELOG entry
not a task"""
        specs = parse_markdown(text, BASE, "claude")
        self.assertEqual([s.title for s in specs], ["Fix flaky test", "Add dark mode toggle", "Write a CHANGELOG entry"])
        self.assertEqual(specs[0].prompt, "Make tests/test_api.py pass reliably")
        self.assertEqual(specs[1].executor, "cursor")
        self.assertEqual(specs[1].priority, 2)
        self.assertEqual(specs[1].cwd, str((BASE / "web").resolve()))
        self.assertEqual(specs[2].executor, "claude")
        self.assertEqual(specs[2].prompt, "Write a CHANGELOG entry")

    def test_markdown_rejects_unknown_executor(self):
        with self.assertRaises(ValueError):
            parse_markdown("- [ ] @gemini do it", BASE, "claude")


class TestImportersJson(unittest.TestCase):
    def test_json_array_and_object(self):
        specs = parse_json('[{"prompt": "Do A\\nmore", "executor": "cursor"}]', BASE, "claude")
        self.assertEqual(specs[0].title, "Do A")
        self.assertEqual(specs[0].executor, "cursor")
        self.assertEqual(specs[0].cwd, str(BASE))

        specs = parse_json('{"tasks": [{"prompt": "B", "title": "Bee", "priority": 3}]}', BASE, "claude")
        self.assertEqual(specs[0].title, "Bee")
        self.assertEqual(specs[0].priority, 3)


if __name__ == "__main__":
    unittest.main()