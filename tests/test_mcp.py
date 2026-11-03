import io
import json

from budgetq.config import Config, DEFAULTS, _merge
from budgetq.mcp import Server, serve


def make(tmp_path):
    cfg = Config(home=tmp_path, data=_merge(DEFAULTS, {"cursor": {"enabled": False}}))
    return cfg, Server(cfg)


def call(server, name, args, msg_id=1):
    reply = server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": args}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_handshake_and_tool_list(tmp_path):
    _, server = make(tmp_path)
    init = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert {"add_task", "add_tasks", "list_tasks", "budget_status", "recent_runs", "update_task"} <= names


def test_add_and_list(tmp_path):
    _, server = make(tmp_path)
    payload, err = call(server, "add_task", {"prompt": "Fix the bug\nin foo.py", "cwd": str(tmp_path), "priority": 2})
    assert not err and payload["queued"]["title"] == "Fix the bug" and payload["queued"]["executor"] == "claude"
    payload, _ = call(server, "list_tasks", {})
    assert [t["title"] for t in payload["tasks"]] == ["Fix the bug"]


def test_add_rejects_bad_input(tmp_path):
    _, server = make(tmp_path)
    assert call(server, "add_task", {"prompt": "x", "cwd": "relative/path"})[1]
    assert call(server, "add_task", {"prompt": "x", "cwd": str(tmp_path / "missing")})[1]
    assert call(server, "add_task", {"prompt": "x", "cwd": str(tmp_path), "executor": "gemini"})[1]


def test_add_tasks_is_all_or_nothing(tmp_path):
    _, server = make(tmp_path)
    payload, err = call(server, "add_tasks", {"tasks": [{"prompt": "ok", "cwd": str(tmp_path)}, {"prompt": "", "cwd": str(tmp_path)}]})
    assert err and "task 2" in payload["error"]
    assert call(server, "list_tasks", {})[0]["tasks"] == []
    payload, err = call(server, "add_tasks", {"tasks": [{"prompt": "a", "cwd": str(tmp_path)}, {"prompt": "b", "cwd": str(tmp_path), "executor": "cursor"}]})
    assert not err and len(payload["queued"]) == 2


def test_update_and_status(tmp_path):
    _, server = make(tmp_path)
    task_id = call(server, "add_task", {"prompt": "a", "cwd": str(tmp_path)})[0]["queued"]["id"]
    assert call(server, "update_task", {"id": task_id, "action": "skip"})[0]["status"] == "skipped"
    assert call(server, "update_task", {"id": 999, "action": "skip"})[1]
    status, err = call(server, "budget_status", {})
    assert not err and status["cursor"] == {"enabled": False} and status["queue"]["skipped"] == 1
    assert status["claude"]["may_run"] is True  # no data yet, unknown_quota=allow


def test_serve_loop_over_stdio(tmp_path):
    cfg, _ = make(tmp_path)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "nope"},
    ]
    out = io.StringIO()
    serve(cfg, io.StringIO("\n".join(json.dumps(l) for l in lines) + "\nnot json\n"), out)
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[1]["error"]["code"] == -32601 and replies[2]["error"]["code"] == -32700
