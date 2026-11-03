# budgetq

Run a backlog of coding tasks through **Claude Code** and the **Cursor CLI** in the background, and stop automatically before your plan quota runs out, so the rest stays free for your own work.

- **Queue:** add tasks one at a time, or import a Markdown checklist or a JSON file. Stored in SQLite.
- **Executors:** `claude -p` (your Claude Pro/Max login) and Cursor's `agent -p` (your Cursor login).
- **Budget gate:** before every task, budgetq reads your real usage and only starts the task if the executor is under your threshold (default 80%). While over, it waits until the window resets, then carries on.
- **Ledger:** every run is logged with its outcome, estimated cost, tokens and the quota before and after.

No API keys, no proxy, no runtime dependencies beyond Python 3.11+.

## Quickstart

```bash
uv tool install git+https://github.com/TeddySabatier/budgetq   # or: pipx install git+https://github.com/TeddySabatier/budgetq
cd my-project
budgetq init
budgetq import backlog.md
budgetq status
budgetq run
```

`budgetq run` drains the queue and exits when it is empty. Leave it running in a terminal, or schedule it (cron, Task Scheduler) with `budgetq run -n 5`.

## Writing a backlog

```markdown
- [ ] @claude Fix flaky test :: Make tests/test_api.py pass reliably, then commit
- [ ] @cursor(cwd=../web, priority=2) Add a dark-mode toggle to the settings page
- [ ] Update the CHANGELOG for the last 10 commits
```

`@claude` / `@cursor` picks the executor (default: `runner.default_executor`). Text after `::` is the prompt; without it the title is the prompt. `cwd` is the folder the agent works in, relative to the backlog file. Higher `priority` runs first. Checked items are ignored.

JSON works too: `[{"prompt": "...", "title": "...", "executor": "cursor", "cwd": "../web", "priority": 1}]`.

One-offs: `budgetq add "Rename foo to bar across the repo" -e claude -C ../repo`.

## Commands

| Command | What it does |
|---|---|
| `budgetq init [dir] [--probe/--no-probe]` | write `budgetq.toml` and create the queue in `.budgetq/` |
| `budgetq add PROMPT [-e claude\|cursor] [-C dir] [-p N]` | queue one task |
| `budgetq import FILE` | queue tasks from `.md` or `.json` |
| `budgetq status` | quota per executor, gate decision, spend this week, queue counts |
| `budgetq run [--once] [-n N] [--only claude\|cursor] [--dry-run]` | drain the queue |
| `budgetq list [-s STATUS]` / `budgetq log [-v]` | tasks / recent runs with cost and usage |
| `budgetq retry ID...` / `budgetq skip ID...` | requeue / skip tasks |
| `budgetq pause` / `budgetq resume` | stop or allow runners from another terminal |

## How usage is read

### Claude (subscription)

| Figure | Where it comes from |
|---|---|
| 5-hour window, weekly window | `rate_limit_event` lines Claude Code prints while a task runs (`--output-format stream-json`) |
| All windows at any time, weekly per model (Opus/Sonnet), extra usage | Optional probe of `api.anthropic.com/api/oauth/usage` (`probe_oauth_usage = true`) |
| Cost per task | `total_cost_usd` from Claude Code: what the run would cost at API prices. On a subscription you are not billed this; it is a measure of how heavy the task was. |

Without the probe, budgetq only knows the quota once a task has run, and Claude Code only reports the window that is currently relevant. With it, `budgetq status` shows everything up front.

Both thresholds are fractions: `five_hour = 0.8` means "don't start a task once the 5-hour window is 80% used". A task already running is only stopped (and requeued) if a window reaches `abort_mid_run_at` (default 95%) or Claude rejects a request.

### Cursor

| Figure | Where it comes from |
|---|---|
| Percent of included usage (total, Auto pool, API pool), billing cycle, on-demand spend | `cursor.com/api/usage-summary`, the endpoint the Cursor dashboard uses |
| Usage per task | the difference between a reading before and after the task |
| "This week" | budgetq's own readings since Monday (Cursor's pools reset monthly, not weekly) |

budgetq reads the login token the Cursor IDE (`state.vscdb`) or Cursor CLI already stored on your machine. You can also set `BUDGETQ_CURSOR_TOKEN`.

The default gate is the `total` pool at 80%. If your API pool is full and on-demand is off, named models stop working but `auto` still does: set `model = "auto"` in `[cursor]`.

## Caveats (read these)

- **Both usage endpoints are undocumented.** They are what Claude Code's `/usage` and the Cursor dashboard call, and they can change without notice. budgetq treats an unreadable or unexpected response as "unknown", and by default pauses Cursor and lets Claude run until it has a reading (`unknown_quota`).
- **Tokens are only read, never refreshed or sent anywhere except their own provider.** Refresh tokens rotate; a second refresher can log Claude Code or Cursor out. If a token has expired, open the app (or run any `claude` command) and budgetq picks up the new one.
- **Terms of service.** Anthropic's plans assume "ordinary, individual usage" of Claude Code, and its terms restrict third-party tools that intermediate claude.ai credentials. budgetq runs the unmodified `claude` binary with your own login; the OAuth probe reads your token to call Anthropic's endpoint, which is why it is opt-in. Decide for yourself whether unattended use fits your plan.
- **Agents act autonomously.** Claude runs with `permission_mode = "acceptEdits"` by default and Cursor with `--force`. Point tasks at a git checkout or worktree, not at anything you can't roll back. Tighten `allowed_tools` for Claude.

## Configuration

`budgetq init` writes a commented `budgetq.toml`. The main keys:

```toml
[claude.thresholds]
five_hour = 0.8
seven_day = 0.8

[cursor.thresholds]
total = 80

[claude]
max_turns = 40
max_budget_usd = 5.0        # per-task cap on Claude Code's API-price estimate
probe_oauth_usage = false

[runner]
poll_seconds = 300
task_timeout_seconds = 3600
```

Data lives in `.budgetq/` next to `budgetq.toml`: `budgetq.db` (queue, runs, quota readings) and `runs/` (full output of each run). Set `BUDGETQ_HOME` to use one queue from anywhere.

## Development

```bash
uv run --extra dev pytest
```

## License

MIT
