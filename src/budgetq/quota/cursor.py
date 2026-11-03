"""Cursor account usage, read from the cursor.com dashboard API.

The endpoint is the one the Cursor dashboard itself calls. It is undocumented
and has changed shape before, so every parse failure is reported as an error
(and the gate pauses) rather than read as zero usage.

Auth: the access token the Cursor IDE or Cursor CLI already stored locally.
budgetq only reads it; it never refreshes or stores it.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import urllib.error
from pathlib import Path

from ..http import get_json
from . import QuotaState, Window, to_epoch

SUMMARY_URL = "https://cursor.com/api/usage-summary"
TOKEN_KEY = "cursorAuth/accessToken"


def _app_data() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def token_sources() -> list[Path]:
    base = _app_data()
    return [
        base / "Cursor" / "User" / "globalStorage" / "state.vscdb",  # Cursor IDE
        base / "Cursor" / "auth.json",  # Cursor CLI
        Path.home() / ".config" / "cursor" / "auth.json",  # Cursor CLI (Linux)
    ]


def _read_vscdb(path: Path) -> str | None:
    uri = f"{path.as_uri()}?mode=ro"
    for suffix in ("", "&immutable=1"):  # immutable avoids lock errors while Cursor runs
        try:
            con = sqlite3.connect(uri + suffix, uri=True, timeout=2)
            try:
                row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (TOKEN_KEY,)).fetchone()
            finally:
                con.close()
            return row[0] if row else None
        except sqlite3.Error:
            continue
    return None


def read_access_token() -> str | None:
    env = os.environ.get("BUDGETQ_CURSOR_TOKEN")
    if env:
        return env
    for path in token_sources():
        if not path.is_file():
            continue
        if path.suffix == ".vscdb":
            token = _read_vscdb(path)
        else:
            try:
                token = json.loads(path.read_text(encoding="utf-8")).get("accessToken")
            except (OSError, ValueError):
                token = None
        if token:
            return token.strip().strip('"')
    return None


def session_cookie(token: str) -> str:
    """Build the dashboard cookie: `<userId>::<jwt>`, URL-encoded."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    sub = json.loads(base64.urlsafe_b64decode(payload))["sub"]
    user_id = sub.split("|")[-1]
    return f"WorkosCursorSessionToken={user_id}%3A%3A{token}"


def _percent(value) -> float | None:
    return float(value) / 100 if isinstance(value, (int, float)) else None


def parse_summary(payload: dict) -> QuotaState:
    cycle_end = to_epoch(payload.get("billingCycleEnd"))
    usage = payload.get("individualUsage") or {}
    plan = usage.get("plan") or {}
    windows: dict[str, Window] = {}

    for name, key in (("total", "totalPercentUsed"), ("auto", "autoPercentUsed"), ("api", "apiPercentUsed")):
        util = _percent(plan.get(key))
        if util is not None:
            windows[name] = Window(utilization=util, resets_at=cycle_end)
    if "total" not in windows and plan.get("limit"):
        windows["total"] = Window(utilization=plan.get("used", 0) / plan["limit"], resets_at=cycle_end)

    on_demand = usage.get("onDemand") or {}
    extra = {
        "membership": payload.get("membershipType"),
        "cycle_start": to_epoch(payload.get("billingCycleStart")),
        "cycle_end": cycle_end,
        "unlimited": payload.get("isUnlimited"),
        "on_demand_enabled": on_demand.get("enabled"),
        "on_demand_used_cents": on_demand.get("used"),
        "on_demand_limit_cents": on_demand.get("limit"),
    }
    error = None if windows else "response has no individualUsage.plan percentages (API changed or team plan)"
    return QuotaState(executor="cursor", source="dashboard", windows=windows, extra=extra, error=error)


def fetch(timeout: float = 10.0) -> QuotaState:
    """Read current-cycle usage from cursor.com. Never raises."""
    token = read_access_token()
    if token is None:
        return QuotaState(executor="cursor", source="dashboard", error="no Cursor login found (sign in to Cursor or run `agent login`)")
    try:
        cookie = session_cookie(token)
    except (IndexError, KeyError, ValueError):
        return QuotaState(executor="cursor", source="dashboard", error="stored Cursor token is not a JWT")
    headers = {
        "Cookie": cookie,
        "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (budgetq)",
    }
    try:
        return parse_summary(get_json(SUMMARY_URL, headers, timeout))
    except urllib.error.HTTPError as exc:
        hint = " (token expired: open Cursor to refresh it)" if exc.code == 401 else ""
        return QuotaState(executor="cursor", source="dashboard", error=f"HTTP {exc.code}{hint}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return QuotaState(executor="cursor", source="dashboard", error=str(exc)[:200])
