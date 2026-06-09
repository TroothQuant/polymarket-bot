# Polymarket project navigation

Before any work that touches files in `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/`, read `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/NAVIGATION.md` first. It documents the folder structure, the file-naming convention (`<type>_<YYYY-MM-DD>.md`, no `NN_` prefix on dated files), and where new files belong. Skipping this step is the failure mode that creates duplicate-prefix and orphan-file drift.

---

# Operating Principles (READ FIRST — overrides everything else in this file)

These two rules govern every session. They override conflicting guidance below.

## 1. Communicate like Jonathon is a beginner

- Jonathon does not code. He does not understand terminal language, file paths, shell syntax, build tools, or developer jargon by default.
- Every response that asks him to do something must use **numbered steps**.
- Every command must sit inside its own copy-pasteable code block — never run two commands on one line joined by `&&` unless he asks for that.
- Every command must be preceded by one or two sentences in plain English explaining (a) what the command does and (b) which terminal tab he should run it in.
- Avoid jargon. If a technical term is unavoidable, define it in one sentence the first time it appears in the session. Examples of terms to define on first use: `PATH`, `port`, `process`, `commit`, `dry run`, `kill`, `source`, `rc file`, `WAL`, `mount`.
- Never write "just run X" or "as you know" or "obviously" — every instruction needs context.
- Default to showing commands one at a time with a confirmation step between them. Only batch when Jonathon explicitly says so.
- When something goes wrong, lead with a plain-English explanation of what happened before the fix. Don't paste raw error messages without translating them.

## 2. Operate autonomously

- Default to **acting and then reporting**. Do not ask for approval on routine operational decisions.
- Use best judgment informed by: the bot's documented edge strategy, the current portfolio state, today's research and briefing, and what is most likely to keep both bots **healthy and profitable**.

### Decisions to make WITHOUT asking
- Closing redundant or highly correlated positions to free capital
- Tuning per-cycle thresholds (Kelly fraction, position caps, category caps, stop-loss percentage, min edge)
- Applying code patches that don't change trading semantics (bug fixes, dedup logic, dashboard fixes, scheduler improvements)
- Restarting bots after a patch
- Choosing which of two similar positions to keep
- Picking which file location to write outputs to
- Deleting duplicate database rows (always with a backup written first)
- Picking the right format / chart settings / log verbosity

### Decisions that REQUIRE asking first
- Moving from paper trading to live trading (real money)
- Sending money or initiating any transfer
- Sending email on Jonathon's behalf to third parties
- Changing the bot's core strategy archetype (e.g. switching from edge-based to momentum-based)
- Killing an entire trading category permanently
- Irreversible deletion of source code, git history, or backups
- Any action with legal or financial implications beyond paper-trading tuning

### How to track progress
- Report what was done, not what is planned. Past tense.
- If intent or scope is ambiguous (rare), one targeted clarifying question at the start of the session is fine. Once scope is clear, execute without re-asking.

---

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket trading bot that estimates fair market probabilities via an AI ensemble (Anthropic, Gemini, OpenAI, OpenRouter, or Azure OpenAI), finds mispricing, and executes trades on Polymarket with Kelly criterion sizing. The agent pays for its own inference from its bankroll.

Two implementations: **Python** (`python/`) and **.NET 8** (`dotnet/PolymarketBot/`). Both share the same logic, config, and data formats.

## Operational state (added 2026-05-19, end-of-day)

Current risk-sizing config (in `polymarket_bot_config.json`):
- `kelly_fraction: 0.15` (was 0.20)
- `max_position_pct: 0.10` (was 0.15)
- `max_category_exposure_pct: 0.25` (was 0.80 — root cause of the original Iran cluster)
- `max_concurrent_positions: 10`

Phase-aware time-to-resolution filter (added `python/main.py` + `config.py`):
- P1 (portfolio < $1K): max 336h (14 days) to resolution; scanner reranks by `volume_24hr / sqrt(hours_to_resolution + 24)` so short-cycle compounding is preferred.
- P2 (portfolio < $5K): max 1080h (45 days).
- P3 (portfolio ≥ $5K): no cap (whale-style hold-to-resolution).

API key lives in `polymarket_bot_config.json` (NOT in shell env) so it survives terminal closes. The `anthropic_api_key` field is loaded by `config.py::from_env()` and used regardless of process env vars.

## Operational state (added 2026-05-23)

### Per-condition_id stop-loss circuit breaker (commit `cc5ff09`)

Addresses the 2026-05-23 Iran NO thrash pattern: 5 stop-losses in 24h across Iran May 26 / May 31 / Jun 30 peace-deal contracts. Market drifted decisively (Jun 30 NO: 0.665 → 0.305 in 8 days) — news the model can't see. Cooldown was working as designed (20 min), then re-entry, then stop, repeat. Bled ~$25 realized.

**Config knobs** (in `polymarket_bot_config.json` + `config.py` defaults, "Position review / exit" cluster):
- `stop_pause_threshold: 2` — N stops within window before the block fires.
- `stop_pause_window_hours: 24.0` — sliding window for streak counting.
- `stop_pause_extra_hours: 48.0` — fixed pause AFTER the trigger fires (regardless of timing). Closes the loop where two stops 23h apart would otherwise produce only a 1h block.

**Persistent state** (in `PortfolioSnapshot`, mirrors the audit-#20 `recently_closed` pattern at commit `97b8ac5`):
- `stop_streak_by_cid: dict[str, list[float]]` — condition_id → [unix timestamps of stops].
- `blocklisted_until: dict[str, float]` — condition_id → unix expiry time.

Both fields survive restart (read in `persistence.load_snapshot`, written in `persistence.save_snapshot`).

**Exit-reason propagation**: `close_position(condition_id, exit_price, exit_reason=None)` now takes the exit reason. Three call sites updated in `trader.py` (PaperTrader.execute_sell, LiveTrader.execute_sell, LiveTrader.execute_topup_and_sell). Only `exit_reason == "stop_loss"` increments the streak.

**Bypass paths** (do NOT increment the streak):
- `operator_close` — new exit reason for manual operator-driven closes. Used by `scripts/close_iran_no_2026-05-23.py`.
- `ghost` — accounting cleanup via `remove_ghost_position`, not a model error.
- `resolved_won` / `resolved_lost` — go through `resolve_position`, not `close_position`. Logged but inert (no re-entry possible after resolution).

**Test coverage**: 6-test suite (trip, re-trip, monkeypatched expiry, take_profit invalidation, persistence round-trip, lazy-expire cleanup). All passing as of `cc5ff09`.

**Operating note**: the circuit breaker is ADDITIVE — same trades go through as before; only the third buy-back inside the threshold window is blocked. It's a thrash detector, not a profit-strategy change.

### Iran NO cluster operator-closed (2026-05-23 evening)

Both open Iran peace-deal positions closed via `scripts/close_iran_no_2026-05-23.py`:
- May 26 NO @ 0.385 → 0.39 → +$0.29
- Jun 30 NO @ 0.305 → 0.255 → −$3.69
- Net −$3.39 realized, freed $41.57 of capital.

Bot resumed cleanly with `stop_streak_by_cid={}` and `blocklisted_until={}` — confirms `operator_close` bypassed the streak.

## Watch out for SIGTERM-ignoring zombie processes

main.py has been observed ignoring SIGTERM. The "stop" command appears to succeed, but the process keeps running and overwrites portfolio.json on heartbeat (~9 min in), clobbering close-out scripts.

Before declaring the bot "stopped", confirm with:
```
ps aux | grep -i "python.*main.py" | grep -v grep
```
If anything comes back, force-kill with `kill -9 <PID>`. Otherwise a "restart" spawns a NEW process alongside the zombie and both write to the same files.

## The 25% category cap structurally fixes wash trades

When the bot closes a position and then re-buys the same condition_id on the next cycle (the "wash trade memory gap" noted in earlier session logs), the new 25% category cap blocks it if other positions in that category already fill the cap. Verified live: bot tried to re-buy "Will the U.S. invade Iran before 2027? NO" within 7 minutes of closing it; the 25% geopolitics cap blocked the wash. Don't relax this cap without thinking about the wash-trade implication.

## Operational notes (added 2026-05-27)

### Category exposure cap tightened from 25% → 15%

Config edit only, no code change. `max_category_exposure_pct` in `polymarket_bot_config.json` lowered from 0.25 to 0.15.

**Why:** today's morning briefing flagged 4 open positions on the same underlying thesis ("short the longshot priced too high") split across two categories — Israel/Hezbollah + US/Iran in `geopolitics`, Spencer Pratt + de la Espriella in `politics`/`other`. The risk limiter caught it and hit "Risk BLOCK" on both categories at the 25% cap. The cluster was net +$3.42 unrealized at the time so this is **preventive**, not corrective — the bot has been making money on the correlated cluster, but the concentration risk is real if a wave of upsets hits multiple longshots at once.

Lowering to 15% forces a more diversified posture without otherwise changing strategy. Picks up effect after the next restart.

### Weather bot day: implications for the Claude bot, none

Today's deep diagnostic was entirely on the weather bot. The Claude bot's per-condition_id stop-loss circuit breaker (`cc5ff09`) is doing its job — no stop-streaks active. Lifetime realized P&L on this bot remains positive (+$62.52 across 41 closed trades plus ~$8-12 unrealized on the open book). Don't reflexively apply weather-bot lessons here; the two bots have different signal sources, different exit logic, and different problem profiles.

## Operational notes (added 2026-06-02)

**As of 2026-06-02 ~13:14-13:16 UTC, the live Claude bot AND the dashboard run on `trooth-prod-nyc3` (DigitalOcean droplet, NYC3), NOT on the Mac.** Mac repo + state + config preserved as fallback at `~/Projects/trooth-claude-bot/` (untouched) and snapshots at `~/Projects/trooth-claude-bot/data/backups/pre_cloud_migration_2026-06-02/`. Cloud-migration session writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-02.md`.

- **SSH into the server:** `ssh trooth-server` (alias, Tailnet-routed). Repo: `/home/trooth/Projects/trooth-claude-bot`. Server HEAD as of cutover: `e5102e5` (one commit ahead of yesterday's pre-cutover state — documented dashboard deps).
- **Live bot unit:** `trooth-claude-bot.service` (enabled, active). `sudo systemctl status trooth-claude-bot` or `sudo journalctl -u trooth-claude-bot -f`. **`WorkingDirectory=/home/trooth/Projects/trooth-claude-bot/python` (the `python/` subdir, NOT the repo root)** — required because `config.data_dir` defaults to `"../data"` which only resolves correctly when CWD is `python/`. ExecStart is `.venv/bin/python python/main.py` (no `--console`). Environment includes `CONFIG_FILE=/home/trooth/.config/trooth/claude.json` and `PYTHONUNBUFFERED=1` (so `print()` calls flush to journal in real time).
- **Live dashboard unit:** `trooth-claude-dashboard.service` (enabled, active). Binds to `127.0.0.1:8001` on the server. Reads JSON/JSONL state files written by the bot; never writes. To view from your Mac: open a Mac Terminal tab and run `ssh -L 8001:localhost:8001 trooth-server`, leave it open, then visit `http://localhost:8001` in the browser. **The Mac dashboard at `~/Projects/trooth-claude-bot/dashboard_server/dashboard.html` is now a stale historical viewer** — Mac state files are frozen at the 2026-06-02 13:14 UTC quiescent point.
- **Server-side state path map:**

  | What | Where |
  |---|---|
  | Repo | `/home/trooth/Projects/trooth-claude-bot/` |
  | venv | `/home/trooth/Projects/trooth-claude-bot/.venv/` (Python 3.12.3) |
  | Live `portfolio.json`, `trades.jsonl`, `snapshots.db` | `/home/trooth/Projects/trooth-claude-bot/data/` |
  | Config (Anthropic key + risk knobs) | `/home/trooth/.config/trooth/claude.json` (mode 600) |
  | `CONFIG_FILE` env | Set in systemd unit to the path above |

- **Rollback procedure (next 7 days):**

  ```
  ssh trooth-server "sudo systemctl disable --now trooth-claude-bot trooth-claude-dashboard"
  ~/Projects/trooth-claude-bot/scripts/run_paper.sh     # Mac Tab 2
  ~/Projects/trooth-claude-bot/scripts/run_dashboard.sh # Mac Tab 3
  open http://localhost:8001                            # browser, no tunnel needed for Mac-local
  ```

- **One commit shipped to `origin/master` to support the cutover:** `e5102e5` — `chore(deps): document dashboard server deps in python/requirements.txt`. Adds `fastapi`, `uvicorn[standard]`, `httpx` floors. They were already running in the Mac venv (so soak-tested) but never written into the requirements file.
- **Overnight auto-restarts are expected and OK.** Ubuntu's `unattended-upgrades` ran at 06:44-06:50 UTC on 6/2 and `needrestart` auto-restarted the weather bot. Same will apply to the Claude bot + dashboard. systemd's `Restart=on-failure` handles it; you'll see a recent "started" timestamp in `systemctl status` on a morning check. Not a regression.

## Operational notes (added 2026-06-05)

Audit remediation (CRITICALs #1/#2/#3/#6/#7 + HIGHs #8/#10/#11/#18/#25 on this bot). All shipped to the live server with per-file backups (`*.bak_*_20260605`). No entry/sizing/exit-strategy change beyond #6.

- **`resolve_position` signature CHANGED** — now `resolve_position(condition_id, outcome: str)` where `outcome in {"won","lost","void"}` (was `(condition_id, won: bool)`). Void pays `0.5 × shares` and touches **neither** the stop-streak **nor** the HWM. Raises `ValueError` on an unknown outcome. The single caller in `main.py` was updated. **If you add a caller, pass the string, not a bool.**
- **Settlement-detection fallback (#1)** — `market_scanner.check_market_resolution` falls back to gamma `/markets?condition_ids={cid}&closed=true` on a CLOB 404, returning `winning_side`, `{"status":"void"}`, or `{"status":"unknown_delisted"}` (logged + skipped for manual review). `get_market_price` now logs 404=info / 5xx=warning / other=error instead of swallowing at debug (closes HIGH #18).
- **SIGTERM handled (#3)** — `signal.SIGTERM` now runs the same graceful shutdown as SIGINT; `systemctl stop/restart` saves state cleanly.
- **Count caps recalibrated for $1,500 (#6)** — `max_concurrent_positions=10→6`; new config knob **`min_position_pct=0.04`** (in `claude.json` + `config.py`), enforced in `portfolio.py` as `floor = max(min_trade_usd, min_position_pct × portfolio_value)`; sub-floor trades are skipped (logged), not rounded up. Size band ≈ $58–$146 at current pv. `max_position_pct=0.10` unchanged.
- **SQLite WAL + busy_timeout=5000 (#8)** — via `_apply_sqlite_pragmas` in `persistence.py`. State files now mode 600 (`os.umask(0o077)` at boot + `os.chmod` after writes) (#11). Gemini key moved to `x-goog-api-key` header (#10). Vestigial `EnvironmentFile=-` removed from the systemd unit (#25).
- **CONFIG_FILE gotcha** — when testing config load by hand, `export CONFIG_FILE=/home/trooth/.config/trooth/claude.json` first, or `BotConfig.from_env()` silently falls back to dataclass defaults (the server unit sets it, so the live service is always correct).

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-05.md`.

## Operational notes (added 2026-06-08)

- **NEVER restart the bot while Anthropic credits are exhausted.** `main.py` `sys.exit(1)`s when provider validation fails at startup → systemd `Restart=on-failure` crash-loop → the price-based exits (stop-loss/take-profit) stop running too. A running-but-blind bot still protects open positions; a down bot protects nothing. The bot ran blind Fri 20:54 UTC → Sun 21:39 UTC (15,775 billing-400 errors) and exits fired correctly throughout. Patches needed during an outage: put them on disk, restart only after credits are confirmed.
- **Void-gap settlement fix shipped + committed (`51f61ff`).** `check_market_resolution` previously only used the gamma fallback on CLOB 404; a CLOB **200 closed=true with no winner flag** (50-50 voids, and ALL sports markets — their token outcomes are team names, never YES/NO) returned None forever. Now falls through to `_resolve_via_gamma`. Verified live: stuck Dota 2 void settled 2s into the first post-restart cycle (+$9.16 payout, −$0.09 PnL).
- **Deployed == version-controlled as of `51f61ff`** (Friday's audit patches + void-gap, all reviewed hunk-by-hunk before commit). Server tree is clean; only dated `.bak_*` rollback snapshots remain untracked.
- **The server's GitHub deploy key is READ-ONLY.** To publish server-side commits: commit on the server, then from the Mac `git fetch trooth-server:/home/trooth/Projects/trooth-claude-bot <branch>` and `git push origin FETCH_HEAD:<branch>`. Used for master (`51f61ff`), and for `sports-bot-v1` (published at `7f74ce4`, NOT merged — merge decision waits for the sports final GO call at n≈40–50).
- **Sports bot: CONDITIONAL GO at n=21** (57.1% hit / +25.80% ROI / +$73.59 favorable gap — variance still dominates). All 5 Phase-5 gate fixes shipped on `sports-bot-v1` (62 tests). Ledger unit now runs an ExecStartPre Elo refresh (9.8s) before the 13:00 UTC settle+log; `--log` prints `Last Elo update`. Never run `--log` manually in the evening against the real CSV — in-game prices produce absurd edges.

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-08.md`.

## Operational notes (added 2026-06-09)

The Claude bot is now on a **numbers-driven probation** (weather is primary; Claude carries API cost + slow capital velocity). Review/kill date **2026-07-07** (or +30 in-window closes). Charter: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/claude_bot_probation_charter_2026-06-09.md`. Today shipped the fixes the probation tests.

- **`stop_pause_threshold` is now 1** (was 2) in `claude.json`. One stop on a `condition_id` blocklists it for the pause window — no re-entry allowed. Tuned after the 6/8→6/9 overnight Yemen thrash (−$55: bot re-entered a one-way YES drop). Don't relax without re-reading the thrash math.
- **90s protective inner-review (`c22d38d`).** New config knob **`review_interval_seconds=90`** (config.py + claude.json). The inter-cycle sleep now calls `run_protective_review()` every 90s — price-refresh + stop-loss/take-profit/edge-gone exits + tiny-position topups — so exits fire between the 10-min full scans. Fixes the gap-through overshoot (stops were filling −30/−43% vs the −25% line because the only price check was once per 10-min cycle). **Purely "look more often" — no exit-threshold/strategy change.** Don't confuse `review_interval_seconds` (protective exits, 90s) with `scan_interval_minutes` (full scan+estimate+entry, 10 min). `run_protective_review` is currently a nested closure inside `main()` — queued to promote to module-level + unit-test.
- **AI protect-only degraded mode (`c22d38d`).** A failed AI validation at startup no longer `sys.exit(1)`s (that crash-looped under systemd `Restart=on-failure` during the credit outage and took the price-based exits down with it). Instead it sets `ai_available=False`, emails a degraded alert (`notify_ai_degraded`), and runs **protect-only** cycles (balance sync + position review + ghost + resolution + stop/TP exits — NO scan/estimate/new trades), re-testing the provider each cycle and auto-resuming (`notify_ai_recovered`) when credits return. **This removes the "never restart while credits are dry" footgun** — the bot now self-protects through an outage. (The 6/8 no-restart rule still applies to OLDER deployments without this patch.)
- **#16 stop-streak contract hardened (`c22d38d`).** `close_position` streak bookkeeping: only exact `"stop_loss"` increments; only `take_profit*`/`phased_take_profit*`/`resolved_won` clear; every other reason (operator_close, ghost, edge_gone, max_hold_timeout_*, resolved_lost, None) is an explicit documented no-op. Repo's **first checked-in tests**: `tests/test_stop_streak_contract.py` (7, run with `.venv/bin/python -m pytest tests/`). Note: the cc5ff09 "6-test suite" referenced above was never actually committed — this is the first test coverage.
- **Probation scorecard (`212a7c1`).** `scripts/claude_probation_scorecard.py` — READ-ONLY on bot data (only writes its own `~/.local/state/trooth/claude_probation.csv`). Per-trade P&L is DERIVED (`shares*exit_price − size_usd`; no `pnl` field on trade records). Weekly `trooth-claude-probation.timer` (Mon 13:30 UTC). Two fix-signature columns: `window_reentry_stop_pnl` (breaker gauge → should crater to ~0) and `window_first_stop_avg` (overshoot gauge → should rise from ~−$18 toward ~−$12). Baseline locked 2026-06-09: realized −$62.02, API cost $124, 51 closes. **Lifetime loss is 100% stop-losses** (+$321 take-profits vs −$401 stops; 76% first-stop / 24% re-entry).
- **Dashboard Realized-P&L filter (`681b205`).** `dashboard_server/dashboard.html` Realized tile has Bot (All/Claude/Weather) + Platform (All/Polymarket/Kalshi) chips, computed from data already on the page. *All/Polymarket* = the clean ex-Kalshi number. **Heads-up for editing dashboard.html via `ssh "...heredoc..."`: backticks and `$` get locally command-substituted inside the double-quoted ssh arg — author JS locally + scp, or avoid template literals.**

Full writeup: `~/Desktop/TROOTH/TROOTH - FINANCIAL/Polymarket/session_log_2026-06-09.md`.

## Running

### Config file (primary)

All settings live in **`polymarket_bot_config.json`** at the project root (gitignored — contains secrets). See `polymarket_bot_config.json.example` for the full annotated template.

Minimum for paper trading:

```json
{
  "anthropic_api_key": "sk-ant-...",
  "anthropic_api_host": "https://api.anthropic.com",
  "anthropic_model": "claude-sonnet-4-6",
  "gamma_api_host": "https://gamma-api.polymarket.com",
  "clob_host": "https://clob.polymarket.com"
}
```

Config priority (highest wins): **CLI arg → env var → polymarket_bot_config.json → code default**

### Python

```bash
cd python
pip install -r requirements.txt
python main.py           # paper trading
python main.py --verbose # debug logging
python main.py --console # human-readable CLI prints
```

### .NET

```bash
cd dotnet/PolymarketBot
dotnet run               # paper trading
dotnet run -- --verbose  # debug logging
dotnet run -- --console  # human-readable CLI prints
```

### Windows

Double-click `run-bot.bat` — reads `polymarket_bot_config.json` automatically.

### CLI risk overrides

```bash
python main.py --max-position-pct 0.15 --max-total-exposure-pct 0.90 --daily-stop-loss-pct 0.20
dotnet run -- --max-position-pct 0.15 --max-total-exposure-pct 0.90 --daily-stop-loss-pct 0.20
```

Available: `--max-position-pct`, `--max-total-exposure-pct`, `--max-category-exposure-pct`, `--daily-stop-loss-pct`, `--max-drawdown-pct`, `--max-concurrent-positions`, `--verbose`, `--console`.

No test suite or linter configured.

## Architecture

### Python (`python/`)

```text
python/
  main.py            – Orchestration loop
  config.py          – BotConfig — per-provider fields, backward compat for claude_model/ai_model
  estimator.py       – Multi-provider AI ensemble: Anthropic/OpenAI/Gemini/OpenRouter/Azure
  notifier.py        – HTML email notifications
  models.py          – Domain dataclasses
  market_scanner.py  – Gamma API pagination, market filtering, CLOB price quotes
  portfolio.py       – Kelly sizing, risk limits, cooldown, ghost removal, position review
  trader.py          – PaperTrader + LiveTrader + ghost detection
  persistence.py     – Atomic JSON portfolio + JSONL trade log
  logger_setup.py    – Colored console + JSON lines file logger
  requirements.txt   – Python dependencies (requests, anthropic, py-clob-client)
```

### .NET (`dotnet/PolymarketBot/`)

```text
dotnet/PolymarketBot/
  Program.cs               – Async orchestration loop
  BotConfig.cs             – Config — per-provider fields, backward compat
  Models/                  – Enums, domain models
  Services/
    Estimator.cs           – Multi-provider AI ensemble (EstimateAsync, EstimateMultiAsync, ValidateApiKeyAsync)
    MarketScanner.cs       – Gamma API + spread filter
    Portfolio.cs           – Kelly sizing, risk checks, cooldown, ghost removal
    Notifier.cs            – HTML email notifications
    ClobApiClient.cs       – EIP-712 + HMAC CLOB auth, orders, auto-claim
    ITrader.cs / LiveTrader.cs / PaperTrader.cs
    PersistenceService.cs  – Atomic JSON + JSONL
    JsonFileLoggerProvider.cs
```

**Data flow per cycle:**

1. **Balance sync** — fetch on-chain USDC, sync bankroll
2. **Ghost check** — verify on-chain token balances; write off positions with < 0.1 tokens
3. **Position review** — fetch prices, run exits (stop-loss/take-profit/edge-gone), optionally re-estimate, topup-and-sell tiny positions
4. `MarketScanner.Scan()` → filtered `MarketInfo` list (liquidity, volume, spread, price, time)
5. `Estimator.Estimate()` → `Estimate` (single or multi-provider ensemble, trimmed mean, confidence filter)
6. `Portfolio.GenerateSignal()` → `Signal` when edge > `min_edge`
7. `Portfolio.CheckRisk()` → 5-layer risk + cooldown
8. `PaperTrader/LiveTrader.Execute()` → `Trade` + `Position`
9. `Persistence` → save snapshot + append trade

**External APIs:**

- Gamma API (`gamma-api.polymarket.com/events`) — market discovery
- CLOB API (`clob.polymarket.com`) — price quotes + live orders
- Anthropic / OpenAI / Gemini / OpenRouter / Azure API — AI estimation

## Key Design Decisions

- **Multi-provider AI estimation** — `multi_provider: true` queries ALL configured providers simultaneously. Each provider gets `ceil(ensemble_size / num_providers)` calls. Scored by `conviction × confidence` (conviction = |estimate - market_price|, confidence = 1/(std_dev + 0.01)). Final estimate = trimmed mean of per-provider means. Bot stops only if ALL providers fail validation.
- **Per-provider model fields** — `anthropic_model`, `openai_model`, `gemini_model`, `openrouter_model` are fully independent. No fallback between providers. Defaults: Anthropic=`claude-sonnet-4-6`, OpenAI=`gpt-4o`, Gemini=`gemini-2.0-flash`.
- **Per-provider `*_enabled` flags** — each provider has `anthropic_enabled`, `gemini_enabled`, `openai_enabled`, `openrouter_enabled`, `azure_openai_enabled` (default true). A provider is only included if BOTH `*_enabled: true` AND its API key is set. Checked in `_get_configured_providers()` (Python) and `GetConfiguredProviders()` (.NET).
- **No legacy `claude_model`/`ai_model` fields** — removed from codebase. JSON values are still read for backward compat (populate `anthropic_model`), but don't create new configs with them.
- **API key validation at startup** — both implementations make a minimal 1-token call per configured provider. Multi mode logs `✓`/`✗` per provider; continues if at least one passes.
- **Provider rate-limit cooldown** — in multi-provider mode (.NET), if a provider exhausts all 429 retries for a market, it's added to `_rateLimitedThisCycle` (HashSet) and skipped instantly for all remaining markets that cycle. `ResetCycle()` clears it at the start of each new cycle. Prevents one rate-limited provider from adding 70+ seconds of retry delays per cycle.
- **Bug fix: ParseProviderResponse** — was always using `_config.AiProvider` to decide parse format (always parsed as anthropic in multi-mode). Now takes `provider` string parameter. This caused azure_openai responses to be parsed as Anthropic format → KeyNotFoundException.
- **Config dump at startup** — after the banner, logs 4 sections: `── AI ──`, `── RISK ──`, `── SCAN ──`, `── EXITS ──` with all key parameters. Helps verify which settings are active.
- **Startup email expanded** — `NotifyStarted`/`notify_started` now shows 4 sections: Portfolio (mode/bankroll/positions), AI (provider/ensemble/min_edge), Risk limits (all 6), Scan (interval/markets/liquidity/volume/spread).
- **Binary markets only** — filters out non-binary outcomes
- **Estimator system prompt** shows current market price as a Bayesian prior — Claude is told to treat market consensus as an anchor
- **Anthropic TextBlock safety** — `response.content[0]` can be ThinkingBlock/ToolUseBlock etc. Always use `next(b for b in response.content if hasattr(b, "text"), None)` not `.content[0].text`
- **Ghost position detection** — each cycle (live only), actual on-chain conditional token balance checked. < 0.1 tokens = ghost: written off immediately with `exit_reason="ghost"`, email notification
- **Position cooldown** — after any close (stop-loss/take-profit/edge-gone/resolved/ghost), blocks re-entry for 2 scan cycles. In-memory, resets on restart. Prevents flip-flopping.
- **Re-estimation during review** — if price moved > `review_reestimate_threshold_pct` (10%), re-run AI with `review_ensemble_size` calls to refresh `fair_estimate_at_entry`
- **CLOB minimum pre-check** uses `price + 0.02` (aggressive price after 2-tick BUY adjustment), not raw market price. Prevents calling AI only to fail at order execution.
- **Tick size** — CLOB `/tick-size` API may return `Number` or `String` JSON. Always handle both value kinds.
- **Confidence filter** — if ensemble std dev > `max_estimate_std` (10%), skip market: `SKIP (low confidence)`
- **Spread filter** — `max_spread = 0.04`: skip markets with wide bid-ask spreads
- **Gamma API JSON quirk** — `outcomes`, `outcomePrices`, `clobTokenIds` can be JSON-encoded strings or actual arrays
- **Risk is layered** — 5 layers: per-position (15%), per-category (80%), total exposure (100%), daily stop-loss (20%), max drawdown (50%). Plus cooldown (6th layer).
- **Config file** `polymarket_bot_config.json` at project root. `CONFIG_FILE` env var overrides path. Priority: CLI arg → env var → config file → code default
- **HTML email notifications** — all events use color-coded HTML templates. Events: started, trade, sell, topup+sell, ghost_removed, resolved, halted, daily_reset, error, stopped
- **CLI args** override env vars/config for risk params
- **Agent pays for inference** — API token costs deducted each cycle
- **Atomic persistence** — portfolio.json written via tmp+rename
- **Polygon chain** (chain ID 137) for Polymarket settlement
- **Live trading** uses GTC limit orders. BUY = midpoint + 2 ticks (taker aggression). SELL = midpoint − 2 ticks. Poll 5×3s for MATCHED status, cancel if unfilled.
- **Top-up-and-sell** for tiny positions (< 5 tokens): buy 5 tokens, then sell all
- **Agent survival** — estimation stops at `bankroll < $0.30`; scan skips when bankroll too low for minimum position; truly halts at `bankroll + total_exposure < $1`. `IsHalted` auto-clears on restart if portfolio healthy.
- **Scan skip threshold** = `max(MinTradeUsd, MaxPositionPct × bankroll)` — free cash only
- **.NET Estimator** uses raw HttpClient to provider REST APIs (no SDK for non-Anthropic providers). Python uses `anthropic` SDK for Anthropic, `requests` for others.
- **.NET CLOB auth** implements EIP-712 signing + HMAC-SHA256 using Nethereum.Signer
- **Auto-claim** (.NET only) — WON position detected → `ClobApiClient.RedeemWinningPositionAsync()` submits raw EIP-155 tx to Polygon
- **Azure OpenAI config note** — `azure_openai_deployment` must match the deployment name exactly (e.g. `gpt-4o-mini`). Without it, azure_openai is excluded from `GetConfiguredProviders()`.

## Dashboard (`dashboard/`)

Electron desktop app for real-time bot monitoring.

### Dashboard Running

```bash
# Windows: double-click run-dashboard.bat (detaches electron, closes CMD immediately)
# Or:
cd dashboard && npm install && npm start
```

### Files

```text
dashboard/
  main.js            Main process: IPC handlers, file watchers, bot process management, fetch-ai-models handler
  preload.js         Context bridge (exposes api.* including fetchAiModels)
  renderer.js        All UI logic: stats, tables, charts, log, per-provider config sections
  index.html         UI shell
  styles.css         Dark/light theme
  package.json       electron ^33.0.0 devDependency
  setup-icon.js      Icon generator — run once: `node setup-icon.js`. Generates icon.png (256×256, Polymarket blue #1652F0, white "P", rounded corners) using pure Node.js (zlib + manual PNG encoding).
  [runtime]          dashboard-settings.json — created at runtime in bot root (next to polymarket_bot_config.json). Stores persistent settings (lang, theme, panel sizes, bot options).
```

### Config Editor — Provider Sections

The config form is organized into per-provider sections: AI PROVIDER, ANTHROPIC, OPENAI, GEMINI, OPENROUTER, AZURE OPENAI. Each provider section has its own API Key, API Host, and Model field.

Model fields use `type: 'model-select'` with a **↺ Load** button. The `loadFrom` property (not `providers`) tells the button which provider API to call for model loading. The `providers` property on AI PROVIDER section fields is for show/hide logic only.

The `fetch-ai-models` IPC handler in `main.js` calls each provider's live model API using Node `fetch()`.

### Key Patterns

- **Bot spawn**: `shell: false` for direct `.exe` path. `shell: true` for `python`/`dotnet run`.
- **Log isolation**: `logClearedAt = Date.now()` on load hides pre-existing entries.
- **Log rotation**: `bot.log` → `bot-TIMESTAMP.log` before each new bot start.
- **Log copy button**: `⎘ copy` button in log controls (next to export). Copies current visible log lines to clipboard. Shows `✓` for 1.5s as confirmation. No new IPC channel needed (clipboard API).
- **Timestamp normalization**: `parseTs(ts)` handles .NET's 7-decimal `ToString("o")`.
- **Charts**: `animation: false` init; `chart.update('none')` — no flicker.
- **FileShare (.NET)**: `new FileStream(..., FileShare.ReadWrite)` for concurrent dashboard + bot access.
- **Stale exe**: after .NET changes, `dotnet build -c Debug` from `dotnet/PolymarketBot/`.
- **File watcher**: 300ms debounce + `name === null` fallback.
- **`t` variable shadowing**: `refresh()` must use `[p, tr, l]` not `[p, t, l]`.
- **i18n**: `TRANS = { ru:{}, en:{} }` + `t(key,...args)`. Text-node update in `applyLang()`.
- **Tooltips**: single `position:fixed` div in `<body>` — avoids `overflow:hidden` clipping.
- **Settings persistence**: `dashboard-settings.json` in bot root, read/written via IPC `read-settings`/`write-settings`. Replaces localStorage. Loaded async at boot before `initTheme`/`initLang`/`setupResize`. Persists: `lang`, `theme`, `bot-mode`, `bot-verbose`, `bot-console`, `panel-left-w`, `panel-upper-h`.
- **Panel sizes persist**: `dragResize` has `onDone` callback. Raw `newW`/`newH` values saved via `setSetting` on mouseup. Restored in `setupResize` before wiring drag handlers.
- **No-terminal launch**: `run-dashboard.bat` uses `start "" "electron.exe" .` to detach electron as a separate process and immediately close CMD. Alternative: `run-dashboard.vbs` for a truly hidden launch.
- **Icon**: `dashboard/setup-icon.js` — run once to generate `icon.png`. Referenced in `BrowserWindow` `icon` option.
- **Rate-limit cooldown** (.NET): `_rateLimitedThisCycle` (HashSet) tracks providers that exhausted 429 retries for a market. Skipped instantly for remaining markets. Cleared by `ResetCycle()` at start of each cycle.

### IPC Channels

`read-portfolio`, `read-trades`, `read-logs`, `read-config`, `write-config`, `get-data-dir`, `set-data-dir`, `browse-data-dir`, `bot-status`, `start-bot`, `stop-bot`, `save-file`, `open-logs-dir`, `fetch-ai-models`, `read-settings`, `write-settings`

Push events (main → renderer): `file-changed`, `bot-output`, `bot-stopped`
