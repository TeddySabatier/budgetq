"""Claude subscription quota.

Primary source: the `rate_limit_event` lines Claude Code prints in
`--output-format stream-json` mode. They cost nothing and need no token
handling, but only describe the window that is currently relevant.

Optional source (off by default): Anthropic's undocumented OAuth usage
endpoint, which returns every window (5-hour, weekly, weekly per model,
extra usage). budgetq only *reads* the access token Claude Code stored; it
never refreshes it, because refresh tokens rotate and a second refresher
can log Claude Code out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from ..http import get_json
from . import QuotaState, Window, to_epoch

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
WINDOW_KEYS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")


def apply_rate_limit_event(event: dict, previous: QuotaState | None) -> QuotaState:
    """Merge one stream-json `rate_limit_event` into the last known state."""
    info = event.get("rate_limit_info") or {}
    windows = dict(previous.windows) if previous else {}
    status = info.get("status")

    unified = info.get("unifiedWindows") or info.get("unified_windows")
    if isinstance(unified, dict):
        for name, data in unified.items():
            if isinstance(data, dict):
                windows[name] = _event_window(data, windows.get(name))
    name = info.get("rateLimitType") or info.get("rate_limit_type")
    if name:
        windows[name] = _event_window(info, windows.get(name))

    return QuotaState(executor="claude", source="stream", windows=windows, status=status)


def _event_window(data: dict, previous: Window | None) -> Window:
    util = data.get("utilization")
    if util is not None:
        util = float(util)
        if util > 1.0:  # some builds report a percentage
            util /= 100
    resets = to_epoch(data.get("resetsAt") or data.get("resets_at"))
    if util is None and previous is not None and resets == previous.resets_at:
        util = previous.utilization
    return Window(utilization=util, resets_at=resets)


def credentials_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".credentials.json" if base else Path.home() / ".claude" / ".credentials.json"


def read_access_token() -> str | None:
    """Read Claude Code's OAuth access token if present and unexpired.

    On macOS Claude Code keeps credentials in the Keychain; budgetq falls back to
    the `security` CLI there.
    """
    raw = None
    path = credentials_path()
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
    elif sys.platform == "darwin":
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
        )
        raw = proc.stdout if proc.returncode == 0 else None
    if not raw:
        return None
    oauth = json.loads(raw).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = to_epoch(oauth.get("expiresAt"))
    if not token or (expires is not None and expires <= time.time() + 60):
        return None
    return token


def parse_usage(payload: dict) -> QuotaState:
    windows = {}
    for key in WINDOW_KEYS:
        data = payload.get(key)
        if isinstance(data, dict) and data.get("utilization") is not None:
            windows[key] = Window(
                utilization=float(data["utilization"]) / 100,
                resets_at=to_epoch(data.get("resets_at")),
            )
    extra = {}
    extra_usage = payload.get("extra_usage")
    if isinstance(extra_usage, dict):
        extra = {
            "extra_usage_enabled": extra_usage.get("is_enabled"),
            "extra_usage_used": extra_usage.get("used_credits"),
            "extra_usage_limit": extra_usage.get("monthly_limit"),
        }
    return QuotaState(executor="claude", source="oauth", windows=windows, extra=extra)


def probe(timeout: float = 5.0) -> QuotaState:
    """Fetch the full quota from the OAuth usage endpoint. Never raises."""
    token = read_access_token()
    if token is None:
        return QuotaState(executor="claude", source="oauth", error="no valid Claude Code token (run claude once)")
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": "budgetq",
        "Accept": "application/json",
    }
    try:
        return parse_usage(get_json(USAGE_URL, headers, timeout))
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("Retry-After") if exc.headers else None
        state = QuotaState(executor="claude", source="oauth", error=f"HTTP {exc.code}")
        if retry and retry.isdigit():
            state.extra["retry_after"] = int(retry)
        return state
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return QuotaState(executor="claude", source="oauth", error=str(exc)[:200])
