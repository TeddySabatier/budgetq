import base64
import json

from budgetq.executors.claude import classify
from budgetq.executors.cursor import usage_delta
from budgetq.quota import QuotaState, Window, to_epoch
from budgetq.quota.claude import apply_rate_limit_event, parse_usage
from budgetq.quota.cursor import parse_summary, session_cookie


def test_to_epoch_formats():
    assert to_epoch(1_767_225_600) == 1_767_225_600
    assert to_epoch(1_767_225_600_000) == 1_767_225_600
    assert to_epoch("2026-01-01T00:00:00Z") == 1_767_225_600
    assert to_epoch(None) is None and to_epoch("nope") is None


def event(**info):
    return {"type": "rate_limit_event", "rate_limit_info": info}


def test_rate_limit_event_merges_windows():
    s1 = apply_rate_limit_event(event(status="allowed", rateLimitType="five_hour", utilization=0.4, resetsAt=100), None)
    s2 = apply_rate_limit_event(event(status="allowed", rateLimitType="seven_day", utilization=0.7, resetsAt=200), s1)
    assert s2.windows["five_hour"].utilization == 0.4
    assert s2.windows["seven_day"].utilization == 0.7


def test_rate_limit_event_keeps_previous_utilization_when_missing():
    s1 = apply_rate_limit_event(event(rateLimitType="five_hour", utilization=0.4, resetsAt=100), None)
    s2 = apply_rate_limit_event(event(status="allowed", rateLimitType="five_hour", resetsAt=100), s1)
    assert s2.windows["five_hour"].utilization == 0.4
    s3 = apply_rate_limit_event(event(status="allowed", rateLimitType="five_hour", resetsAt=999), s2)
    assert s3.windows["five_hour"].utilization is None  # new window, old figure no longer applies


def test_rate_limit_event_unified_windows():
    s = apply_rate_limit_event(
        event(status="allowed", unifiedWindows={"five_hour": {"utilization": 0.2, "resetsAt": 5}, "seven_day": {"utilization": 0.6, "resetsAt": 9}}),
        None,
    )
    assert s.summary() == {"five_hour": 0.2, "seven_day": 0.6}


def test_parse_oauth_usage():
    s = parse_usage(
        {
            "five_hour": {"utilization": 12.0, "resets_at": "2026-11-03T15:00:00Z"},
            "seven_day": {"utilization": 63.5, "resets_at": "2026-11-07T10:00:00Z"},
            "seven_day_opus": None,
            "extra_usage": {"is_enabled": True, "monthly_limit": 2000, "used_credits": 150},
        }
    )
    assert s.windows["five_hour"].utilization == 0.12
    assert s.windows["seven_day"].utilization == 0.635
    assert "seven_day_opus" not in s.windows
    assert s.extra["extra_usage_used"] == 150


SUMMARY = {
    "billingCycleStart": "2026-10-08T10:14:33.000Z",
    "billingCycleEnd": "2026-11-08T10:14:33.000Z",
    "membershipType": "pro",
    "isUnlimited": False,
    "individualUsage": {
        "plan": {"autoPercentUsed": 58.5, "apiPercentUsed": 100, "totalPercentUsed": 59.7},
        "onDemand": {"enabled": False, "used": 0, "limit": None},
    },
}


def test_parse_cursor_summary():
    s = parse_summary(SUMMARY)
    assert s.error is None
    assert round(s.windows["total"].utilization, 3) == 0.597
    assert s.windows["api"].utilization == 1.0
    assert s.extra["on_demand_used_cents"] == 0
    assert s.windows["total"].resets_at == to_epoch("2026-11-08T10:14:33Z")


def test_parse_cursor_summary_without_plan_is_an_error():
    s = parse_summary({"membershipType": "team", "teamUsage": {}})
    assert s.error and not s.windows


def test_parse_cursor_summary_legacy_cents():
    s = parse_summary({"individualUsage": {"plan": {"used": 1000, "limit": 2000}}})
    assert s.windows["total"].utilization == 0.5


def test_session_cookie():
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "auth0|user_abc"}).encode()).decode().rstrip("=")
    token = f"h.{payload}.sig"
    assert session_cookie(token) == f"WorkosCursorSessionToken=user_abc%3A%3A{token}"


def test_classify_claude_result():
    assert classify({"subtype": "success", "is_error": False, "result": "done"}) == ("success", "done")
    # seen in the wild: an expired login reports subtype "success" with is_error true
    expired = {"subtype": "success", "is_error": True, "terminal_reason": "api_error",
               "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}
    assert classify(expired)[0] == "auth_error"
    assert classify({"subtype": "error_max_turns", "is_error": True})[0] == "error_max_turns"
    assert classify({"subtype": "success", "is_error": True, "terminal_reason": "api_error", "result": "boom"})[0] == "api_error"


def test_usage_delta():
    before = QuotaState("cursor", "dashboard", 0, {"total": Window(0.5)}, extra={"cycle_start": 1, "on_demand_used_cents": 0})
    after = QuotaState("cursor", "dashboard", 0, {"total": Window(0.512)}, extra={"cycle_start": 1, "on_demand_used_cents": 30})
    assert usage_delta(before, after) == {"total_pct": 1.2, "on_demand_cents": 30}
    after.extra["cycle_start"] = 2
    assert "note" in usage_delta(before, after)
