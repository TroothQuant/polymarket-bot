# Bot Memory

Running notes between Claude Code sessions. Not a changelog — just current state, known issues, and context useful to restore quickly.

---

## Current State (as of 2026-03-19)

- **Mode:** LIVE on Polygon (chain ID 137)
- **Wallet:** Gnosis Safe (`polymarket_signature_type: 1`)
- **Active implementation:** .NET (run via `run-bot.bat` or `dotnet run -- --console`)
- **Dashboard:** `run-dashboard.bat`

---

## Key Features (all implemented, both Python and .NET)

### API Key Validation (startup)

Both implementations now validate the Anthropic API key at startup with a minimal 1-token call. HTTP 401 → log error + exit immediately. Network/rate-limit errors are warned and ignored (transient, don't block startup).

- Python: `estimator.validate_api_key()` — raises `anthropic.AuthenticationError` on 401
- .NET: `estimator.ValidateApiKeyAsync()` — checks HTTP 401 status

### Ghost Position Detection

Each cycle (live trading only), tracked positions are verified against actual on-chain token balances via CLOB `/balance-allowance`. If on-chain balance < 0.1 tokens, the position is a ghost (failed order, partial fill, or cleanup issue). Written off immediately:

- Trade logged with `exit_reason = "ghost"`, price = 0, loss = full cost basis
- Email notification sent (purple color in HTML template)
- `_recently_closed` cooldown entry added (prevents immediate re-entry)
- No manual intervention needed

### Position Cooldown

After closing any position (stop-loss, take-profit, edge-gone, resolved, or ghost), the bot blocks re-entering that same market for `scan_interval_minutes × 2` seconds (2 cycles). Prevents flip-flopping on noisy signals.

- Tracked via `_recently_closed: dict[str, float]` (condition_id → close timestamp)
- Not persisted — resets on restart (intentional)

### Re-estimation During Position Review

If a position's price moves more than `review_reestimate_threshold_pct` (default 10%) since entry, Claude is re-queried with a smaller `review_ensemble_size` ensemble (default 3) to refresh the fair value estimate. This updates `fair_estimate_at_entry` before edge-gone exit logic runs, so exits are based on current Claude opinion rather than stale entry estimate.

### Confidence Filter

After ensemble estimation, if std dev of Claude's estimates exceeds `max_estimate_std` (default 10%), the market is skipped with `SKIP (low confidence)`. Claude's ensemble is disagreeing too much to act on.

### Spread Filter

Markets with bid-ask spread > `max_spread` (default 4¢) are skipped during scanning. Wide spreads indicate thin liquidity and poor fill quality.

### HTML Emails

All email notifications use HTML templates with color-coded event types. Each event type has a distinct color (green=buy, red=sell/loss, yellow=warning, purple=ghost, etc.).

---

## Recent Fixes & Implementation Notes

### SELL Order Bugs Fixed (.NET)

- **Floor not Round**: `Math.Floor(shares * 100) / 100` — rounding can exceed on-chain balance by atomic units
- **SELL price aggression**: subtract 2 ticks from midpoint (mirrors BUY's +2 ticks) for immediate taker fill
- **Balance sync lag**: after SELL, CLOB balance API shows stale USDC — corrects next cycle. Expected.
- **CLOB `/balance-allowance`**: returns `allowances` (plural), not `allowance`. Max uint256 = already approved.

### Scan Threshold Fix

Scan skip threshold = `max(MinTradeUsd, MaxPositionPct × bankroll)` — based on free cash only, not portfolio value. Prevents false blocks when most capital is locked in open positions.

### Auto-claim (.NET only)

When a WON position is detected, submits raw EIP-155 tx to Polygon calling `CTF.redeemPositions`. Config required: `ctf_address`, `usdc_address`, `polygon_rpc_url`. Controlled by `auto_claim` (default true).

### SKIP Log Clarity

`Program.cs` distinguishes two null-signal reasons:

- Edge IS sufficient but position size is below CLOB minimum → logs "SKIP (bankroll < min)", console "TOO SMALL: need $X, have $Y"
- Edge genuinely below threshold → logs "SKIP (no edge)"

---

## Architecture Reminders

- Config priority: CLI arg → env var → `polymarket_bot_config.json` → code default
- `polymarket_bot_config.json` is gitignored (contains private key + API keys)
- `polymarket_bot_config.json.example` — annotated template with recommended values
- Kelly sizing caps at `min(KellyFraction × Kelly%, MaxPositionPct × portfolioValue)`, then checked against `bankroll`
- Balance sync: on-chain USDC fetched every cycle and after each trade
- `IsHalted` auto-clears on restart if `bankroll + TotalExposure() > $1`
- Both Python and .NET must stay in sync — mirror every logic change

---

## Config Defaults (code-level)

| Setting | Default |
|---------|---------|
| `scan_interval_minutes` | 10 |
| `markets_per_cycle` | 20 |
| `min_liquidity` | 10000 |
| `min_volume_24hr` | 500 |
| `min_time_to_resolution_hours` | 48 |
| `max_spread` | 0.04 |
| `ensemble_size` | 3 |
| `max_estimate_std` | 0.10 |
| `min_edge` | 0.10 |
| `kelly_fraction` | 0.20 |
| `min_trade_usd` | 0.5 |
| `max_position_pct` | 15% |
| `max_total_exposure_pct` | 100% |
| `max_category_exposure_pct` | 80% |
| `daily_stop_loss_pct` | 20% |
| `max_drawdown_pct` | 50% |
| `max_concurrent_positions` | 10 |
| `position_stop_loss_pct` | 25% |
| `take_profit_price` | 0.95 |
| `review_reestimate_threshold_pct` | 0.10 |
| `review_ensemble_size` | 3 |
| `auto_claim` | true |
| `polygon_rpc_url` | https://polygon-rpc.com |
