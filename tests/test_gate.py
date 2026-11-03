from budgetq.config import DEFAULTS
from budgetq.gate import decide_claude, decide_cursor
from budgetq.quota import QuotaState, Window

NOW = 1_800_000_000.0
CLAUDE = DEFAULTS["claude"]
CURSOR = DEFAULTS["cursor"]


def claude(windows, status=None):
    return QuotaState("claude", "stream", NOW, windows, status)


def test_claude_unknown_follows_policy():
    assert decide_claude(None, CLAUDE, NOW).open
    assert not decide_claude(None, {**CLAUDE, "unknown_quota": "pause"}, NOW).open


def test_claude_under_threshold_is_open():
    state = claude({"five_hour": Window(0.5, NOW + 3600), "seven_day": Window(0.79, NOW + 86400)})
    assert decide_claude(state, CLAUDE, NOW).open


def test_claude_weekly_over_threshold_pauses_until_reset():
    state = claude({"five_hour": Window(0.1, NOW + 3600), "seven_day": Window(0.8, NOW + 86400)})
    d = decide_claude(state, CLAUDE, NOW)
    assert not d.open and "seven_day" in d.reason and d.until == NOW + 86400


def test_claude_window_that_already_reset_is_ignored():
    state = claude({"five_hour": Window(0.95, NOW - 10)})
    assert decide_claude(state, CLAUDE, NOW).open


def test_claude_rejected_without_figures_pauses():
    state = claude({"five_hour": Window(None, NOW + 600)}, status="rejected")
    d = decide_claude(state, CLAUDE, NOW)
    assert not d.open and d.until == NOW + 600


def test_claude_warning_without_figures_pauses_but_with_figures_trusts_threshold():
    assert not decide_claude(claude({"seven_day": Window(None, NOW + 600)}, "allowed_warning"), CLAUDE, NOW).open
    assert decide_claude(claude({"seven_day": Window(0.5, NOW + 600)}, "allowed_warning"), CLAUDE, NOW).open


def test_claude_stale_rejection_after_reset_opens():
    state = claude({"five_hour": Window(None, NOW - 5)}, status="rejected")
    assert decide_claude(state, CLAUDE, NOW).open


def cursor(total, api=None, used_cents=0, error=None):
    windows = {"total": Window(total, NOW + 86400)}
    if api is not None:
        windows["api"] = Window(api, NOW + 86400)
    return QuotaState("cursor", "dashboard", NOW, windows, extra={"on_demand_used_cents": used_cents}, error=error)


def test_cursor_threshold():
    assert decide_cursor(cursor(0.6, api=1.0), CURSOR, NOW).open  # full API pool doesn't block by default
    d = decide_cursor(cursor(0.8), CURSOR, NOW)
    assert not d.open and d.until == NOW + 86400


def test_cursor_error_pauses_by_default():
    assert not decide_cursor(cursor(0.1, error="HTTP 403"), CURSOR, NOW).open
    assert decide_cursor(cursor(0.1, error="HTTP 403"), {**CURSOR, "unknown_quota": "allow"}, NOW).open


def test_cursor_on_demand_cap():
    cfg = {**CURSOR, "on_demand_cap_usd": 5}
    assert decide_cursor(cursor(0.5, used_cents=499), cfg, NOW).open
    assert not decide_cursor(cursor(0.5, used_cents=500), cfg, NOW).open
