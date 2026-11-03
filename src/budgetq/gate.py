"""Budget gate: decide whether an executor may start another task."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .quota import QuotaState


@dataclass
class Decision:
    open: bool
    reason: str
    until: float | None = None  # when to look again, epoch seconds


def _unknown(policy: str, why: str) -> Decision:
    if policy == "allow":
        return Decision(True, f"{why}; unknown_quota=allow")
    return Decision(False, f"{why}; unknown_quota=pause")


def _windows_over(state: QuotaState, thresholds: dict[str, float], now: float, scale: float):
    """Yield (name, used, limit, resets_at) for every live window at or over its threshold."""
    for name, window in state.windows.items():
        limit = thresholds.get(name)
        if limit is None or window.utilization is None:
            continue
        if window.resets_at is not None and window.resets_at <= now:
            continue  # the window has reset since we last looked
        if window.utilization * scale >= limit:
            yield name, window.utilization * scale, limit, window.resets_at


def decide_claude(state: QuotaState | None, cfg: dict, now: float | None = None) -> Decision:
    now = now or time.time()
    policy = cfg["unknown_quota"]
    if state is None:
        return _unknown(policy, "no Claude quota data yet")

    over = list(_windows_over(state, cfg["thresholds"], now, 1.0))
    if over:
        name, used, limit, resets = max(over, key=lambda o: o[3] or 0)
        return Decision(False, f"Claude {name} at {used:.0%} (limit {limit:.0%})", resets)

    # Claude Code sometimes reports a status without a utilization figure. Trust the
    # status until the window it refers to resets.
    if state.status in ("rejected", "allowed_warning"):
        resets = [w.resets_at for w in state.windows.values() if w.resets_at]
        live = [r for r in resets if r > now]
        has_figures = any(w.utilization is not None for w in state.windows.values())
        if live and (state.status == "rejected" or not has_figures):
            return Decision(False, f"Claude reports status {state.status}", max(live))
        if not resets and state.status == "rejected":
            return Decision(False, "Claude reports status rejected (no reset time given)")
    return Decision(True, "Claude under thresholds")


def decide_cursor(state: QuotaState | None, cfg: dict, now: float | None = None) -> Decision:
    now = now or time.time()
    policy = cfg["unknown_quota"]
    if state is None:
        return _unknown(policy, "no Cursor usage data")
    if state.error:
        return _unknown(policy, f"Cursor usage unreadable ({state.error})")
    if not state.windows:
        return _unknown(policy, "Cursor usage has no plan breakdown")

    over = list(_windows_over(state, cfg["thresholds"], now, 100.0))
    if over:
        name, used, limit, resets = over[0]
        return Decision(False, f"Cursor {name} pool at {used:.0f}% (limit {limit:g}%)", resets)

    cap = cfg.get("on_demand_cap_usd", -1)
    used_cents = state.extra.get("on_demand_used_cents")
    if cap is not None and cap >= 0 and used_cents is not None and used_cents >= cap * 100:
        cycle_end = state.windows.get("total").resets_at if "total" in state.windows else None
        return Decision(False, f"Cursor on-demand ${used_cents / 100:.2f} >= cap ${cap:.2f}", cycle_end)
    return Decision(True, "Cursor under thresholds")


DECIDERS = {"claude": decide_claude, "cursor": decide_cursor}
