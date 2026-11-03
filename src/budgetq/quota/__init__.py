"""Normalized quota state shared by all executors."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class Window:
    utilization: float | None  # 0..1
    resets_at: float | None = None  # epoch seconds


@dataclass
class QuotaState:
    executor: str
    source: str
    fetched_at: float = field(default_factory=time.time)
    windows: dict[str, Window] = field(default_factory=dict)
    status: str | None = None
    extra: dict = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "QuotaState":
        raw = json.loads(text)
        raw["windows"] = {k: Window(**v) for k, v in raw.get("windows", {}).items()}
        return cls(**raw)

    def summary(self) -> dict[str, float | None]:
        return {name: w.utilization for name, w in self.windows.items()}


def to_epoch(value) -> float | None:
    """Accept epoch seconds, epoch milliseconds or ISO-8601 strings."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str):
        if value.isdigit():
            return to_epoch(int(value))
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None
