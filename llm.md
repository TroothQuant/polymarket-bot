# Polymarket Bot — LLM Code Reference

Comprehensive description of the codebase for future Claude sessions.

---

## Project Summary

Autonomous trading bot for Polymarket (binary prediction markets). Each cycle it:

1. Syncs on-chain USDC balance
2. Checks for ghost positions (tracked but no on-chain tokens)
3. Reviews open positions (exit if stop-loss / take-profit / edge-gone / re-estimate, cooldown on re-entry)
4. Scans Gamma API for active markets
5. Estimates fair probability via ensemble of N Claude API calls (trimmed mean + confidence filter)
6. Sizes a position using fractional Kelly criterion
7. Checks 5-layer risk limits + cooldown
8. Executes the trade (paper or live CLOB)
9. Persists state and sends email notification

The bot pays for its own Claude API inference from its bankroll. Two identical implementations: **Python** (`python/`) and **.NET 8** (`dotnet/PolymarketBot/`). Both read the same `polymarket_bot_config.json` and write to the same `data/` directory.

---

## Config (`python/config.py` / `dotnet/BotConfig.cs`)

Single `BotConfig` dataclass. Load priority: **CLI arg → env var → `polymarket_bot_config.json` → code default**.

Key fields and defaults:

**Mode**
- `live_trading: bool = False`
- `initial_bankroll: float = 10000`

**Scanning**
- `scan_interval_minutes: int = 10`
- `markets_per_cycle: int = 20`
- `min_liquidity: float = 10000`
- `min_volume_24hr: float = 500`
- `min_time_to_resolution_hours: float = 48`
- `min_market_price: float = 0.10`
- `max_spread: float = 0.04` — skip markets with bid-ask spread > 4¢

**Estimation**
- `claude_model: str = "claude-sonnet-4-20250514"`
- `ensemble_size: int = 3`
- `ensemble_temperature: float = 0.7`
- `max_estimate_tokens: int = 1024`
- `max_estimate_std: float = 0.10` — skip market if ensemble std dev > 10% (Claude disagrees too much)

**Sizing**
- `min_edge: float = 0.10`
- `kelly_fraction: float = 0.20`
- `min_trade_usd: float = 0.5`

**Risk**
- `max_position_pct: float = 0.15`
- `max_total_exposure_pct: float = 1.00`
- `max_category_exposure_pct: float = 0.80`
- `daily_stop_loss_pct: float = 0.20`
- `max_drawdown_pct: float = 0.50`
- `max_concurrent_positions: int = 10`

**Exits**
- `enable_position_review: bool = True`
- `position_stop_loss_pct: float = 0.25`
- `take_profit_price: float = 0.95`
- `exit_edge_buffer: float = 0.05`
- `review_reestimate_threshold_pct: float = 0.10` — re-run Claude during review if price moved > 10%
- `review_ensemble_size: int = 3` — smaller ensemble for mid-cycle re-estimation

**Capital**
- `data_dir: str = "../data"`
- `auto_claim: bool = True` — (.NET only) auto-claim won positions on-chain

**Config file location**: project root `polymarket_bot_config.json`. Not tracked by git. Path overridden via `CONFIG_FILE` env var.

See `polymarket_bot_config.json.example` for a fully annotated template.

---

## Models (`python/models.py` / `dotnet/Models/`)

All domain types. Both implementations share the same field names.

### MarketInfo

Parsed from Gamma API. Key fields: `condition_id`, `question`, `outcome_yes_price`, `outcome_no_price`, `token_id_yes`, `token_id_no`, `liquidity`, `volume_24hr`, `best_bid`, `best_ask`, `spread`, `end_date`, `category`, `event_title`, `description`.

### Estimate

Output of Claude ensemble. Key fields: `fair_probability` (trimmed mean), `raw_estimates` (list), `confidence` (std dev), `reasoning_summary` (first call's reasoning), `input_tokens_used`, `output_tokens_used`.

### Signal

Generated when edge exceeds threshold. Key fields: `market`, `estimate`, `side` (YES/NO), `edge`, `market_price`, `kelly_fraction`, `position_size_usd`, `expected_value`.

### Position

Open position in portfolio. Key fields: `condition_id`, `question`, `side`, `token_id`, `entry_price`, `size_usd` (cost basis), `shares`, `current_price`, `unrealized_pnl`, `category`, `fair_estimate_at_entry` (original Claude estimate — used for edge-gone exit logic), `order_id`.

### Trade

Completed trade record (BUY or SELL). Key fields: `trade_id`, `condition_id`, `side`, `action` (BUY/SELL), `price`, `size_usd`, `shares`, `is_paper`, `edge_at_entry`, `kelly_at_entry`, `exit_reason`.

`exit_reason` values: `stop_loss`, `take_profit`, `edge_gone`, `resolved_won`, `resolved_lost`, `ghost`.

### ExitSignal

Signals a position should be closed. Fields: `position`, `exit_reason`, `current_price`, `unrealized_pnl`, `pnl_pct`.

### TopupCandidate

Tiny position (<5 tokens) that wants to exit but can't (CLOB minimum is 5 tokens). Fields: `position`, `exit_reason`, `tokens_to_buy=5.0`, `topup_cost` (5 × current_price), `recovery_value` (current shares × current_price).

### PortfolioSnapshot

Persisted state. Fields: `bankroll`, `initial_bankroll`, `positions`, `high_water_mark`, `daily_start_value`, `total_realized_pnl`, `total_trades`, `is_halted`.

---

## Main Loop (`python/main.py` / `dotnet/Program.cs`)

Runs indefinitely, one cycle per `scan_interval_minutes`.

### Startup sequence

1. Parse CLI args → apply overrides to config
2. Setup logging
3. Load portfolio snapshot (or start fresh)
4. Create services: MarketScanner, Estimator, Notifier
5. **Validate Anthropic API key** — make a minimal test call (`max_tokens=1`). Exit if HTTP 401. Other errors (network) are warned and ignored.
6. Initialize live trader (if `live_trading=true`): derive CLOB creds, check token approvals, sync initial USDC balance
7. Enter main loop

### Cycle structure

**1. Halt check** — if `is_halted`, send notification and break.

**2. Daily reset** — if UTC date changed since last cycle, call `portfolio.reset_daily()` (resets `daily_start_value`).

**3. Balance sync** — live trading only: fetch actual on-chain USDC from CLOB API, call `portfolio.sync_balance()`. Corrects drift from fees, partial fills, resolved positions.

**4. Position review** (if `enable_position_review` and positions exist):

- Fetch current midpoint prices for all held tokens
- Update `current_price` and `unrealized_pnl` on all positions

  **Ghost check**: For each position, verify actual on-chain conditional token balance via CLOB `/balance-allowance`. If balance < 0.1 tokens, the position is a ghost (failed order or partial fill). Write it off: `portfolio.remove_ghost_position()`, log GHOST trade with `exit_reason="ghost"`, notify via email.

  **Tier 0 — Resolved markets**: For positions where price is missing or < $0.01, query CLOB `/markets/{condition_id}`. If closed, call `portfolio.resolve_position(condition_id, won)`. Won: payout = shares × $1. Lost: payout = $0. .NET: also submits auto-claim on-chain tx.

  **Tier 1 — Rule-based exits**: `portfolio.generate_exit_signals()` checks each position: stop-loss (PnL% < -25%), take-profit (price ≥ 0.95), edge-gone (price > fair_estimate + buffer). Optionally re-estimates if price moved > `review_reestimate_threshold_pct` since entry (uses smaller `review_ensemble_size` ensemble). For each ExitSignal, calls `trader.execute_sell()`.

  **Tier 1.5 — Topup-and-sell**: `portfolio.generate_topup_candidates()` finds tiny (<5 token) positions that meet exit conditions. Calls `trader.execute_topup_and_sell()`. Skipped if bankroll < topup cost.

  **Cooldown tracking**: After any position is closed (close_position, resolve_position, or ghost removal), the condition_id is added to `_recently_closed` with a timestamp. `check_risk()` blocks re-entry for `scan_interval_minutes × 2` seconds (2 cycles).

**5. Scan skip guard** — if `bankroll < max(max_position_pct × bankroll, min_trade_usd)`, skip scan entirely.

**6. Market scan** — `scanner.scan()` fetches active events from Gamma API, filters by liquidity/volume/spread/price/time, returns up to `markets_per_cycle` results sorted by volume.

**7. Exposure capacity check** — pre-check if `exposure_room < min_realistic_position`. If so, skip all estimations.

**8. Market evaluation loop** — for each market:

- Skip if already holding this `condition_id`
- Skip if at capacity
- Skip if `bankroll < $0.30` (API reserve guard)
- Skip if `bankroll < 5 × min(yes_price, no_price)` (can't afford CLOB minimum)
- Call `estimator.estimate(market)` → N Claude calls, trimmed mean, confidence filter
- Call `portfolio.record_api_cost()` → deduct inference cost from bankroll
- If portfolio value < $1, set `is_halted = True`
- Call `portfolio.generate_signal()` → Signal if edge > min_edge
- If signal is None, log why: "SKIP (no edge)" vs "SKIP (bankroll < min)" for size-below-CLOB-minimum case
- Call `portfolio.check_risk()` → validate all risk limits + cooldown
- Call `trader.execute()` → Trade record
- Persist trade + snapshot

**9. Cycle summary** — log stats, save final snapshot.

**10. Sleep** — tick-by-tick (1s ticks) for responsive Ctrl+C shutdown.

---

## MarketScanner (`python/market_scanner.py` / `dotnet/Services/MarketScanner.cs`)

### scan() / ScanAsync()

Paginates through `{gamma_api_host}/events?active=true&closed=false&limit=100&offset=N`. Filters each market:

- `active=true` and `closed=false`
- Exactly 2 binary outcomes
- `liquidity >= min_liquidity`
- `volume_24hr >= min_volume_24hr`
- At least one side price in [min_market_price, 1-min_market_price]
- Time to resolution > `min_time_to_resolution_hours`
- **Spread filter**: `spread > max_spread` → skip (thin liquidity, poor fill quality)

Gamma API quirk: `outcomes`, `outcomePrices`, `clobTokenIds` may be JSON-encoded strings inside JSON — both forms handled.

Category classification uses `CATEGORY_KEYWORDS` dict. Categories: politics, geopolitics, sports, crypto, tech, social_media, weather, entertainment, finance, other.

Markets sorted by `volume_24hr` descending.

---

## Estimator (`python/estimator.py` / `dotnet/Services/Estimator.cs`)

### ValidateApiKeyAsync / validate_api_key

Called once at startup. Makes a minimal API call (`max_tokens=1, content="hi"`). Returns `False` on HTTP 401 (invalid key). Other errors (network, rate-limit) return `True` so transient failures don't block startup. Python raises `anthropic.AuthenticationError` on 401.

### Ensemble approach

Calls Claude `ensemble_size` times independently per market. Temperature 0.7 for diversity. Drops highest and lowest if ≥ 4 samples (trimmed mean). Confidence = standard deviation.

**Confidence filter**: If `confidence > max_estimate_std` (default 10%), the market is skipped — ensemble disagreement too high to act on. Logs `SKIP (low confidence): ... std=X.XXX`.

### System prompt

Asks for calibrated probability. Rules: output only JSON `{"probability": 0.XX, "reasoning": "..."}`, clamped to [0.02, 0.98], keep reasoning under 50 words. The prompt includes the current market price as a Bayesian prior (Claude is told to treat market consensus as an anchor and only deviate with strong reasoning).

### Error handling

- Python: `anthropic.RateLimitError` → sleep 5s, return None
- .NET: 429/529 → exponential backoff 10s → 20s → 40s, up to 3 retries. After max retries returns null (market skipped).
- JSON parse failures → None
- < 2 valid estimates → warning logged; 0 estimates → return None (market skipped)

---

## Portfolio (`python/portfolio.py` / `dotnet/Services/Portfolio.cs`)

### Kelly criterion (generate_signal)

```text
b = (1/market_price) - 1     # decimal odds
p = fair_probability          # YES side
q = 1 - p
kelly_raw = (b*p - q) / b
kelly = kelly_raw * kelly_fraction
size_usd = kelly * portfolio_value
size_usd = min(size_usd, portfolio_value * max_position_pct)
size_usd = min(size_usd, bankroll)
```

Returns None if: edge < min_edge for both sides, market_price ≤ 0 or ≥ 1, size_usd < min_trade_usd, size_usd < 5 × market_price (CLOB minimum of 5 tokens).

### Risk checks (check_risk)

In order:

1. Already holding this market → block
2. **Cooldown**: recently closed this market → block for 2 cycles
3. `len(positions) >= max_concurrent_positions` → block
4. `total_exposure + new_size > portfolio_value × max_total_exposure_pct` → block
5. `category_exposure + new_size > portfolio_value × max_category_exposure_pct` → block
6. Daily stop-loss exceeded → halt
7. Max drawdown exceeded → halt
8. `bankroll + total_exposure < $1` → halt (agent dead)

### Cooldown logic

`_recently_closed: dict[str, float]` — maps condition_id → Unix timestamp when position was closed.

`check_risk()` computes `cooldown_secs = scan_interval_minutes × 60 × 2` (2 cycles). If `time.now() - closed_at < cooldown_secs`, blocks re-entry. Entry is removed from `_recently_closed` when the cooldown expires.

Cooldown entries are added by: `close_position()`, `resolve_position()`, and `remove_ghost_position()`. Not persisted — resets each run (intentional: restarts clear cooldown state).

### Ghost position removal

`remove_ghost_position(condition_id)`: removes position from list, subtracts cost basis from bankroll (full loss), records timestamp in `_recently_closed`. Returns pnl (always = -size_usd).

### Position management

- `open_position()`: deducts `size_usd` from bankroll, appends position
- `close_position(condition_id, exit_price)`: removes position, returns `size_usd + pnl` to bankroll. PnL = `shares × (exit_price - entry_price)`.
- `resolve_position(condition_id, won)`: won → payout = shares ($1 each). lost → payout = 0.
- `add_to_position()`: for topup-and-sell

### Exit signal generation

Iterates all positions, skips `current_price < $0.01` (penny) or `shares < 5` (tiny). Checks: stop-loss → take-profit → edge-gone.

Optionally re-estimates during review: if price moved more than `review_reestimate_threshold_pct` from entry price, runs Claude again with `review_ensemble_size` calls. Updates `fair_estimate_at_entry` on the position if re-estimate succeeds.

### API cost tracking

`record_api_cost(input_tokens, output_tokens)`: deducts `(input × $3/MTok + output × $15/MTok)` from bankroll.

---

## Trader (`python/trader.py` / `dotnet/Services/LiveTrader.cs`, `PaperTrader.cs`)

### Ghost position detection (LiveTrader only)

`verify_positions(positions)` / called from main loop's ghost check section:

For each tracked position, calls CLOB `/balance-allowance?token_id=...` to get actual on-chain conditional token balance. If balance < 0.1 tokens, the position is a ghost (from failed order, partial fill, or expired without proper cleanup). Returns list of ghost condition_ids.

### BUY flow

1. Price = `signal.market_price + 0.02` (2 ticks aggression, capped at 0.99)
2. Create GTC limit order via CLOB
3. Poll for MATCHED status: 5 attempts × 3s = 15s max
4. Not matched → cancel order, return None
5. Matched → parse actual fill amounts, open position

### SELL flow

1. Fetch actual on-chain balance (partial fill correction)
2. Skip if price < $0.01 or shares < 5
3. Submit SELL GTC order at midpoint − 0.02 (2 ticks below, symmetric with BUY aggression)
4. Poll 3 attempts × 2s
5. Matched → `portfolio.close_position()`, return Trade

### TopupAndSell

1. BUY 5 tokens (same GTC polling)
2. `portfolio.add_to_position(5, cost)`
3. Fetch actual on-chain balance
4. SELL all tokens
5. If SELL fails → position now has 5+ tokens, sellable next cycle

### CLOB authentication (.NET ClobApiClient)

- **L1 (CLOB auth)**: Signs `ClobAuth` struct (EIP-712) → derive API keys
- **L2 (Order signing)**: Signs `Order` struct (EIP-712) per order
- **HMAC**: L2 requests include HMAC-SHA256 of timestamp+method+path+body

**Auto-claim**: WON position detected → `RedeemWinningPositionAsync()` submits raw EIP-155 tx to Polygon calling `CTF.redeemPositions`. ABI-encoded calldata (196 bytes), signs with `EthECKey` + Keccak.

---

## Persistence (`python/persistence.py` / `dotnet/Services/PersistenceService.cs`)

- **Portfolio state**: `data/portfolio.json` — atomic write via tmp+rename
- **Trade log**: `data/trades.jsonl` — append-only JSONL
- **Bot log**: `data/bot.log` — JSON lines, opened with `FileShare.ReadWrite` (.NET) so dashboard can read concurrently

---

## Notifier (`python/notifier.py` / `dotnet/Services/Notifier.cs`)

Sends **HTML** emails. All methods silently swallow errors.

Events: `started`, `trade` (BUY), `sell`, `topup_sell`, `ghost_removed`, `resolved` (WON/LOST), `halted`, `daily_reset`, `error`, `stopped`.

Each event type has a distinct color (green/red/yellow/purple/etc.) in the HTML template. SMTP: STARTTLS (port 587) if `email_use_tls=true`, SMTP_SSL (port 465) if false.

---

## Key Invariants and Edge Cases

**Portfolio value**: always `bankroll + total_exposure()`. Bankroll = free USDC; total_exposure = cost basis of open positions.

**CLOB minimum**: 5 tokens per order. Min BUY in USD = `5 × price`. Positions < 5 tokens → TopupAndSell.

**Penny positions**: price < $0.01 — completely unsellable. Skipped in all review checks.

**Ghost positions**: tracked but zero on-chain balance. Written off immediately with full cost basis as loss. `exit_reason = "ghost"` in trade log.

**Bankroll can go negative**: Normal when capital is locked in positions + API costs. Only fatal when `bankroll + total_exposure < $1`.

**Stale is_halted**: Auto-cleared on restart if `bankroll + total_exposure ≥ $1`.

**Scan skip threshold**: `max(min_trade_usd, max_position_pct × bankroll)` — free cash only, not portfolio value.

**Cooldown**: 2 cycles after closing a position before re-entering the same market. Not persisted — clears on restart.

**Balance sync lag**: After live SELL, CLOB balance API shows stale USDC for one cycle. Corrects automatically.

**GTC orders**: BUY at midpoint + 2 ticks (taker aggression). SELL at midpoint − 2 ticks (symmetric). Poll → cancel if unfilled.

**Re-estimation during review**: If price moved > `review_reestimate_threshold_pct` (10%) since entry, Claude is re-queried with `review_ensemble_size` (3) calls. Updates `fair_estimate_at_entry`. Edge-gone exit uses the refreshed estimate.

**API key validation**: Startup makes a 1-token test call. HTTP 401 → exit immediately. Other errors → warn and continue (transient failures shouldn't block startup).

---

## Data Flow Diagram

```text
main.py / Program.cs (main loop)
├── API key validation
│   └── Anthropic API /v1/messages (max_tokens=1)
│
├── scanner.get_market_prices(token_ids)
│   └── CLOB API /midpoint (per token)
│
├── [ghost check] clobClient.GetConditionalBalanceAsync(token_id)
│   └── CLOB API /balance-allowance
│
├── scanner.check_market_resolution(condition_id)
│   └── CLOB API /markets/{condition_id}
│
├── scanner.scan()
│   └── Gamma API /events (paginated)
│       └── parse + filter + spread check → list[MarketInfo]
│
├── estimator.estimate(market)
│   └── N × Anthropic API /v1/messages
│       └── trimmed mean + confidence filter → Estimate
│
├── portfolio.generate_signal(market, estimate)
│   └── Kelly criterion → Signal
│
├── portfolio.check_risk(signal)
│   └── 5-layer risk check + cooldown → bool
│
├── trader.execute / execute_sell / execute_topup_and_sell
│   ├── PaperTrader: in-memory simulation
│   └── LiveTrader: CLOB API /order (GTC) + poll /order/{id}
│
├── persistence.save_snapshot(portfolio.snapshot())
│   └── data/portfolio.json (atomic tmp+rename)
│
└── persistence.append_trade(trade)
    └── data/trades.jsonl (append)
```

---

## File Index

### Python

| File | Purpose |
|------|---------|
| [python/main.py](python/main.py) | Main orchestration loop |
| [python/config.py](python/config.py) | BotConfig dataclass + config loading |
| [python/models.py](python/models.py) | All domain dataclasses and enums |
| [python/market_scanner.py](python/market_scanner.py) | Gamma API + CLOB price/resolution queries |
| [python/estimator.py](python/estimator.py) | Claude ensemble + confidence filter + API key validation |
| [python/portfolio.py](python/portfolio.py) | Kelly sizing, risk checks, cooldown, ghost removal, position review |
| [python/trader.py](python/trader.py) | PaperTrader + LiveTrader + ghost position detection |
| [python/persistence.py](python/persistence.py) | JSON save/load for portfolio + JSONL trades |
| [python/notifier.py](python/notifier.py) | HTML email notifications |
| [python/logger_setup.py](python/logger_setup.py) | Colored console + JSON file logging |

### .NET

| File | Purpose |
|------|---------|
| [dotnet/PolymarketBot/Program.cs](dotnet/PolymarketBot/Program.cs) | Async main loop |
| [dotnet/PolymarketBot/BotConfig.cs](dotnet/PolymarketBot/BotConfig.cs) | Config loading |
| [dotnet/PolymarketBot/Services/MarketScanner.cs](dotnet/PolymarketBot/Services/MarketScanner.cs) | Market discovery + spread filter |
| [dotnet/PolymarketBot/Services/Estimator.cs](dotnet/PolymarketBot/Services/Estimator.cs) | Claude ensemble + confidence filter + API key validation |
| [dotnet/PolymarketBot/Services/Portfolio.cs](dotnet/PolymarketBot/Services/Portfolio.cs) | Kelly sizing, risk checks, cooldown, ghost removal |
| [dotnet/PolymarketBot/Services/LiveTrader.cs](dotnet/PolymarketBot/Services/LiveTrader.cs) | Live trading + ghost detection |
| [dotnet/PolymarketBot/Services/PaperTrader.cs](dotnet/PolymarketBot/Services/PaperTrader.cs) | Simulated execution |
| [dotnet/PolymarketBot/Services/ClobApiClient.cs](dotnet/PolymarketBot/Services/ClobApiClient.cs) | EIP-712 + HMAC CLOB auth + auto-claim |
| [dotnet/PolymarketBot/Services/PersistenceService.cs](dotnet/PolymarketBot/Services/PersistenceService.cs) | Atomic JSON + JSONL |
| [dotnet/PolymarketBot/Services/Notifier.cs](dotnet/PolymarketBot/Services/Notifier.cs) | HTML email notifications |
| [dotnet/PolymarketBot/Services/JsonFileLoggerProvider.cs](dotnet/PolymarketBot/Services/JsonFileLoggerProvider.cs) | JSON line file logger |
| [dotnet/PolymarketBot/Models/](dotnet/PolymarketBot/Models/) | C# model classes |

---

## Common Gotchas When Making Changes

1. **Both implementations must stay in sync** — any logic change in Python should be mirrored in .NET.

2. **Gamma API JSON quirk** — `outcomes`, `outcomePrices`, `clobTokenIds` can be JSON-encoded strings or actual lists. Always handle both.

3. **CLOB order amounts**: BUY `amount` = USD; SELL `amount` = tokens. Asymmetry by design (CLOB convention).

4. **Portfolio value vs bankroll**: Risk limits use `bankroll + total_exposure()`. Never confuse the two.

5. **The agent is NOT dead when bankroll goes negative** — only dead when `bankroll + total_exposure < $1`.

6. **`fair_estimate_at_entry`** is stored per position at trade time. Set to 0 for old/legacy positions (disables edge-gone checks). Updated by re-estimation during position review.

7. **CLOB minimum = 5 tokens** — enforced on both BUY and SELL. Positions under 5 tokens use TopupAndSell.

8. **Cooldown is in-memory only** — resets when bot restarts. This is intentional.

9. **Ghost check runs every cycle** in live mode (not paper). Only checks positions with a valid `token_id`.

10. **API key validation at startup** — makes a real API call (1 token, cheap). Fails fast on 401. Does NOT fail on 429/529/network errors.

11. **.NET Estimator uses raw HttpClient** to Anthropic REST API. Python uses the `anthropic` SDK.

12. **Auto-claim (.NET only)**: When `auto_claim=true` and a WON position is detected, an EIP-155 tx is submitted to Polygon. Failure is non-blocking.

---

## Dashboard (`dashboard/`)

Electron desktop app for real-time bot monitoring. Read-only except for `write-config`.

### Architecture

- **main.js** — Node.js main process. File I/O, child process management, IPC, `fs.watch` file watchers.
- **preload.js** — Context bridge. `contextBridge.exposeInMainWorld('api', ...)`.
- **renderer.js** — All UI logic.
- **index.html** / **styles.css** — UI shell and dark/light themes.

### Main Process (main.js)

- `findDataDir()` — `../data` relative to `dashboard/`, fallback to `userData/data`
- `setupFileWatcher()` — watches `data/` for changes; 300ms debounce; `name === null` fallback
- `readLines(file, n)` — reads last N lines of a JSONL file

**start-bot handler**:
1. Rotates `bot.log` → `bot-TIMESTAMP.log`
2. Looks for pre-compiled exe: `bin/Release/net8.0/PolymarketBot.exe` then `bin/Debug/...`
3. If exe: `spawn(exePath, extraArgs, { shell: false })` — critical for paths with spaces
4. No exe: `spawn('dotnet', ['run', ...], { shell: true })`
5. Python: `spawn('python', ['main.py', ...], { shell: true })`

### Renderer (renderer.js)

Key patterns:

- **`t` variable bug**: `refresh()` must destructure as `[p, tr, l]` not `[p, t, l]` — `t` is the global translation function; shadowing it causes TypeError.
- **Log isolation**: `logClearedAt = Date.now()` on load hides pre-existing entries. Reset to 0 + clear arrays on bot start.
- **Charts**: `animation: false` on init; `chart.update('none')` on update — no flicker.
- **Tooltips**: Single `position: fixed` div in `<body>` — avoids `overflow: hidden` clipping.
- **i18n**: `TRANS = { ru: {}, en: {} }` + `t(key, ...args)`. `applyLang()` uses text-node update (not `innerHTML`).
- **Theme**: `body.light` CSS class. `localStorage.theme`.

### Dashboard Known Issues / Fixed Bugs

- **Path with spaces**: `shell: false` for direct exe paths (Windows CMD splits at space).
- **FileShare**: .NET `StreamWriter` fixed with `new FileStream(..., FileShare.ReadWrite)` in `Program.cs`.
- **Stale exe**: After .NET source changes, must `dotnet build -c Debug` from `dotnet/PolymarketBot/`.
- **7-decimal timestamps**: `parseTs()` strips extra fractional-second digits (.NET's `ToString("o")`).
- **File watcher on Windows**: `name === null` fallback + 300ms debounce.
- **CSS tooltip clipping**: JS-positioned `<div class="tooltip-popup">` in `<body>` with `position: fixed`.
