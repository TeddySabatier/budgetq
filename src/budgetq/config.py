"""Configuration: budgetq.toml discovery, defaults and merging."""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "budgetq.toml"
DATA_DIR = ".budgetq"

DEFAULTS: dict = {
    "runner": {
        "default_executor": "claude",
        "poll_seconds": 300,
        "task_timeout_seconds": 3600,
    },
    "claude": {
        "enabled": True,
        "command": ["claude"],
        "model": "",
        "max_turns": 40,
        "max_budget_usd": 5.0,
        "permission_mode": "acceptEdits",
        "allowed_tools": [],
        "extra_args": [],
        "unknown_quota": "allow",
        "abort_mid_run_at": 0.95,
        "probe_oauth_usage": False,
        "probe_min_interval_seconds": 600,
        "thresholds": {
            "five_hour": 0.8,
            "seven_day": 0.8,
            "seven_day_opus": 0.8,
            "seven_day_sonnet": 0.8,
        },
    },
    "cursor": {
        "enabled": True,
        "command": [],
        "model": "",
        "force": True,
        "extra_args": [],
        "unknown_quota": "pause",
        "thresholds": {"total": 80},
        "on_demand_cap_usd": -1,
    },
}

DEFAULT_TOML = """\
# budgetq configuration. Every key is optional; missing keys use the defaults shown.

[runner]
default_executor = "claude"   # executor for tasks that don't name one: claude | cursor
poll_seconds = 300            # how often to re-check quotas while paused
task_timeout_seconds = 3600   # kill a task that runs longer than this

[claude]
enabled = true
command = ["claude"]          # how to launch Claude Code
model = ""                    # e.g. "sonnet"; empty = Claude Code default
max_turns = 40                # per-task turn cap
max_budget_usd = 5.0          # per-task cap on Claude Code's API-price estimate; 0 = off
permission_mode = "acceptEdits"  # acceptEdits | bypassPermissions | default | plan
allowed_tools = []            # e.g. ["Bash(git *)", "Bash(npm test)"]
extra_args = []
unknown_quota = "allow"       # no quota data yet: allow | pause
abort_mid_run_at = 0.95       # stop a running task (and requeue it) if any window reaches this
probe_oauth_usage = PROBE     # read the full quota (weekly, per-model, extra usage) from
                              # Anthropic's undocumented OAuth usage endpoint. See README.
probe_min_interval_seconds = 600

[claude.thresholds]           # pause when a window's utilization (0-1) reaches this
five_hour = 0.8
seven_day = 0.8
seven_day_opus = 0.8
seven_day_sonnet = 0.8

[cursor]
enabled = true
command = []                  # empty = auto-detect the Cursor CLI (`agent`)
model = ""                    # e.g. "auto"; empty = Cursor default
force = true                  # pass --force so the agent can write files and run commands
extra_args = []
unknown_quota = "pause"       # usage can't be read: allow | pause

[cursor.thresholds]           # pause when a pool's percent used reaches this
total = 80
# auto = 90                   # Auto/Composer pool
# api = 90                    # named-model (API-priced) pool; once it is full, named
                              # models bill on-demand while "auto" keeps working

# Pause once on-demand (paid overage) spend this cycle reaches this many USD; -1 = off.
# on_demand_cap_usd = -1
"""


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    home: Path
    data: dict

    @property
    def data_dir(self) -> Path:
        return self.home / DATA_DIR

    @property
    def db_path(self) -> Path:
        return self.data_dir / "budgetq.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def section(self, name: str) -> dict:
        return self.data[name]


def find_home(start: Path | None = None) -> Path | None:
    env = os.environ.get("BUDGETQ_HOME")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / CONFIG_NAME).is_file():
            return folder
    return None


def load(home: Path) -> Config:
    path = home / CONFIG_NAME
    # utf-8-sig: Windows editors often save a BOM, which tomllib rejects
    user = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    return Config(home=home, data=_merge(DEFAULTS, user))


def write_default(home: Path, probe: bool) -> Path:
    path = home / CONFIG_NAME
    path.write_text(DEFAULT_TOML.replace("PROBE", "true" if probe else "false"), encoding="utf-8")
    return path
