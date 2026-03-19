# Polymarket Bot — LLM Code Reference

Comprehensive description of the codebase for future Claude sessions.

---

## Project Summary

Autonomous trading bot for Polymarket (binary prediction markets). Each cycle it:

1. Syncs on-chain USDC balance
2. Checks for ghost positions (tracked but no on-chain tokens)
3. Reviews open positions (exit if stop-loss / take-profit / edge-gone / re-estimate, cooldown on re-entry)
4. Scans Gamma API for active markets
5. Estimates fair probability via AI ensemble (any of: Anthropic, OpenAI, Gemini, OpenRouter, Azure OpenAI)
6. Sizes a position using fractional Kelly criterion
7. Checks 5-layer risk limits + cooldown
8. Executes the trade (paper or live CLOB)
9. Persists state and sends HTML email notification

The bot pays for its own AI inference from its bankroll. Two identical implementations: **Python** (`python/`) and **.NET 8** (`dotnet/PolymarketBot/`). Both read the same `polymarket_bot_config.json` and write to the same `data/` directory.

---

## Config (`python/config.py` / `dotnet/BotConfig.cs`)

Single `BotConfig` dataclass. Load priority: **CLI arg → env var → `polymarket_bot_config.json` → code default**.

### AI Provider fields

- `ai_provider: str = "anthropic"` — active provider for single-provider mode
- `multi_provider: bool = False` — query ALL configured providers and aggregate

Per-provider credentials + models (each provider fully independent):

| Provider | Key | Host | Model | Default model |
|---|---|---|---|---|
| Anthropic | `anthropic_api_key` | `anthropic_api_host` | `anthropic_model` | `claude-sonnet-4-6` |
| OpenAI | `openai_api_key` | `openai_api_host` | `openai_model` | `gpt-4o` |
| Gemini | `gemini_api_key` | `gemini_api_host` | `gemini_model` | `gemini-2.0-flash` |
| OpenRouter | `openrouter_api_key` | `openrouter_api_host` | `openrouter_model` | (set manually) |
| Azure OpenAI | `azure_openai_api_key` | `azure_openai_endpoint` | `azure_openai_deployment` | (set manually) |

Azure also: `azure_openai_api_version` (default `2024-02-01`).

**Backward compat**: loading `claude_model` or `ai_model` from JSON still works — they populate `anthropic_model` as a fallback.

### Scan fields
- `scan_interval_minutes: int = 10`
- `min_liquidity: float = 10000`
- `min_volume_24hr: float = 1000`
- `min_time_to_resolution_hours: float = 48`
- `min_market_price: float = 0.10`
- `markets_per_cycle: int = 15`
- `max_spread: float = 0.04` — skip wide bid-ask spreads

### Estimation fields
- `ensemble_size: int = 3` — total AI calls per market (distributed across providers in multi mode)
- `ensemble_temperature: float = 0.7`
- `max_estimate_tokens: int = 1024`
- `max_estimate_std: float = 0.10` — skip market if ensemble std dev exceeds this

### Sizing / Risk / Exit fields
- `kelly_fraction: float = 0.20`, `min_edge: float = 0.10`, `min_trade_usd: float = 0.5`
- `max_position_pct: float = 0.15`, `max_total_exposure_pct: float = 1.00`, `max_category_exposure_pct: float = 0.80`
- `daily_stop_loss_pct: float = 0.20`, `max_drawdown_pct: float = 0.50`, `max_concurrent_positions: int = 10`
- `position_stop_loss_pct: float = 0.25`, `take_profit_price: float = 0.95`, `exit_edge_buffer: float = 0.05`
- `review_reestimate_threshold_pct: float = 0.10`, `review_ensemble_size: int = 3`

**Config file location**: project root `polymarket_bot_config.json`. Not tracked by git. `CONFIG_FILE` env var overrides path. See `polymarket_bot_config.json.example` for fully annotated template.

---

## Models (`python/models.py` / `dotnet/Models/`)

### MarketInfo
Parsed from Gamma API. Key fields: `condition_id`, `question`, `outcome_yes_price`, `outcome_no_price`, `token_id_yes`, `token_id_no`, `liquidity`, `volume_24hr`, `best_bid`, `best_ask`, `spread`, `end_date`, `category`, `event_title`, `description`.

### Estimate
Output of AI ensemble. Key fields: `fair_probability` (trimmed mean), `raw_estimates` (list), `confidence` (std dev), `reasoning_summary`, `input_tokens_used`, `output_tokens_used`.

### Signal
Generated when edge exceeds threshold. Key fields: `market`, `estimate`, `side` (YES/NO), `edge`, `market_price`, `kelly_fraction`, `position_size_usd`.

### Position
Open position. Key fields: `condition_id`, `question`, `side`, `token_id`, `entry_price`, `size_usd`, `shares`, `current_price`, `unrealized_pnl`, `category`, `fair_estimate_at_entry`, `order_id`.

### Trade
Completed trade. Key fields: `trade_id`, `condition_id`, `action` (BUY/SELL), `price`, `size_usd`, `shares`, `is_paper`, `exit_reason`. Exit reasons: `stop_loss`, `take_profit`, `edge_gone`, `resolved_won`, `resolved_lost`, `ghost`.

### ExitSignal, TopupCandidate, PortfolioSnapshot — as previously documented.

---

## Main Loop (`python/main.py` / `dotnet/Program.cs`)

### Startup sequence
1. Parse CLI args
2. Setup logging
3. Load portfolio snapshot
4. Create services: MarketScanner, Estimator, Notifier
5. **Validate API keys** — `estimator.validate_api_key()`:
   - Single-provider mode: validate the one configured provider. HTTP 401/403 → exit.
   - Multi-provider mode: validate ALL configured providers. Log `✓`/`✗` per provider. Only exits if ALL fail.
6. Initialize live trader if `live_trading=true`
7. Enter main loop

### Cycle structure

**1. Halt check** — if `is_halted`, break.

**2. Daily reset** — `portfolio.reset_daily()` if UTC date changed.

**3. Balance sync** — live only: fetch on-chain USDC, `portfolio.sync_balance()`.

**4. Position review** (if `enable_position_review`):
- Fetch midpoint prices for all held tokens
- **Ghost check**: verify actual on-chain token balance per position. If < 0.1 tokens → ghost: write off, log trade with `exit_reason="ghost"`, email, add to cooldown.
- **Tier 0 — Resolved**: price < $0.01 → check CLOB `/markets/{condition_id}`. Resolved → `portfolio.resolve_position()`. .NET: also auto-claim on-chain.
- **Tier 1 — Rule exits**: `portfolio.generate_exit_signals()` → stop-loss / take-profit / edge-gone. Optionally re-estimates if price moved > `review_reestimate_threshold_pct`.
- **Tier 1.5 — Topup-and-sell**: tiny (<5 token) positions with exit signals → buy 5 tokens, sell all.

**5. Scan skip guard** — skip if `bankroll < max(max_position_pct × bankroll, min_trade_usd)`.

**6. Market scan** — Gamma API → filtered by liquidity/volume/spread/price/time → sorted by volume desc.

**7. Market evaluation loop** — for each market:
- Skip: already held, at capacity, `bankroll < $0.30`, `bankroll < 5 × (bestPrice + 0.02)`
- `estimator.estimate(market)` → Estimate (single or multi-provider)
- `portfolio.record_api_cost()` → deduct inference cost
- `portfolio.generate_signal()` → Signal or None
- Log: "SKIP (no edge)" vs "SKIP (bankroll < min)" (size below CLOB minimum)
- `portfolio.check_risk()` → 5 layers + cooldown
- `trader.execute()` → Trade

**8. Cycle summary** — log stats, save snapshot.

**9. Sleep** — tick-by-tick (1s ticks) for responsive Ctrl+C.

---

## Estimator (`python/estimator.py` / `dotnet/Services/Estimator.cs`)

### Provider dispatch

The Estimator supports 5 providers. Call routing:

```
estimate(market)
  ├── multi_provider=false → _estimate_single() → N calls to self._provider
  └── multi_provider=true  → _estimate_multi()  → ceil(N/P) calls to each configured provider
```

### Single-provider estimation

N calls to the active provider → trimmed mean → confidence filter. Same as before but now works with any provider.

### Multi-provider estimation

1. Detect configured providers: `_get_configured_providers()` — checks for non-empty API keys
2. Each provider gets `ceil(ensemble_size / num_providers)` calls
3. Per-provider: collect `(probs, input_tokens, output_tokens, reasoning)`
4. Score each provider:
   ```
   market_price = (yes_price + (1 - no_price)) / 2
   conviction   = |provider_mean - market_price|
   confidence   = 1 / (std_dev + 0.01)
   score        = conviction × confidence
   ```
5. Log breakdown with `⭐` on highest-scoring provider
6. Final estimate = trimmed mean of per-provider means (equal weight)

### Provider call methods

- **Anthropic** (`_call_anthropic`): uses `anthropic` SDK. Returns token counts from `response.usage`. Finds TextBlock explicitly: `next(b for b in response.content if hasattr(b, "text"), None)`.
- **OpenAI-compatible** (`_call_openai_compat`): handles `openai`, `openrouter`, `azure_openai`. POST `/v1/chat/completions` (or Azure equivalent). Token counts from `usage.prompt_tokens / completion_tokens`.
- **Gemini** (`_call_gemini`): POST `/v1beta/models/{model}:generateContent`. Token counts from `usageMetadata.promptTokenCount / candidatesTokenCount`.

### Model selection per provider

`_get_model(provider)` returns the configured per-provider model field, falling back to hardcoded defaults:
- anthropic → `anthropic_model` or `"claude-sonnet-4-6"`
- openai → `openai_model` or `"gpt-4o"`
- gemini → `gemini_model` or `"gemini-2.0-flash"`
- openrouter → `openrouter_model` (no default)
- azure_openai → `azure_openai_deployment` (required)

### Confidence filter

If `std_dev > max_estimate_std` after collecting all estimates → skip market. Logs `SKIP (low confidence)`.

### API key validation

`validate_api_key()` dispatches based on `multi_provider`:
- Single: validate the one provider, return False on HTTP 401/403
- Multi: validate all configured, return False only if ALL fail. Logs `✓`/`✗` per provider. Warning if some fail.
- Gemini: returns 400 (not 401) for invalid key — check body for "API key" string

### Rate limit handling
- Python: `anthropic.RateLimitError` → sleep 5s
- .NET: HTTP 429/529 → exponential backoff 10s → 20s → 40s (up to 3 retries)
- OpenAI/Gemini/OpenRouter: HTTP 429 → sleep 5s (Python), same backoff (.NET)

---

## Portfolio (`python/portfolio.py` / `dotnet/Services/Portfolio.cs`)

### Kelly criterion (generate_signal)

```text
effectivePrice = marketPrice + 0.02  (add 2-tick aggression for accurate CLOB min check)
minClobUsd = max(5 × effectivePrice, 1.0)
if sizeUsd < minClobUsd → return None (CLOB minimum not met)
```

### Risk checks (check_risk) — in order:
1. Already holding this market → block
2. **Cooldown**: recently closed this market → block for 2 cycles
3. `len(positions) >= max_concurrent_positions` → block
4. Total exposure cap → block
5. Category cap → block
6. Daily stop-loss exceeded → halt
7. Max drawdown exceeded → halt
8. `bankroll + total_exposure < $1` → halt

### Cooldown logic
`_recently_closed: dict[str, float]` — maps `condition_id → unix timestamp of close`. Added by `close_position()`, `resolve_position()`, `remove_ghost_position()`. Cooldown window = `scan_interval_minutes × 60 × 2`. Expires entries when window passes. **Not persisted** — resets on restart.

### Ghost position removal
`remove_ghost_position(condition_id)`: removes from positions, subtracts cost basis from bankroll (full loss), adds to cooldown. Returns PnL (always = `-size_usd`).

### Re-estimation during review
If `current_price` moved > `review_reestimate_threshold_pct` from `entry_price`, calls `estimator.estimate(market)` with `review_ensemble_size` calls. Updates `fair_estimate_at_entry` if successful.

### Exit signal generation
Skips: `current_price < $0.01` (penny) or `shares < 5` (too small). Checks: stop-loss → take-profit → edge-gone.

---

## Trader (`python/trader.py` / `dotnet/Services/LiveTrader.cs`)

### Ghost detection
`verify_positions(positions)` / in Program.cs ghost check loop: fetches actual on-chain conditional token balance via CLOB `/balance-allowance`. Balance < 0.1 tokens → ghost.

### CLOB minimum pre-check
Pre-scan check uses `bestPrice + 0.02` (aggressive price after 2-tick BUY adjustment) to avoid calling the AI only to fail at order execution due to CLOB minimum.

### Tick size parsing
`GetTickSizeAsync` handles both `String` and `Number` JSON value kinds from the CLOB `/tick-size` endpoint.

---

## Persistence, Notifier, ClobApiClient

Same as previously documented. Notifier sends HTML emails with color-coded event types including `ghost_removed` (purple).

---

## Dashboard (`dashboard/`)

### Config Editor — AI Provider sections

The config form is now organized into per-provider sections (no mixing):
- **AI PROVIDER**: `ai_provider` dropdown + `multi_provider` toggle
- **ANTHROPIC** / **OPENAI** / **GEMINI** / **OPENROUTER** / **AZURE OPENAI**: each has its own API Key, API Host, and Model fields

Model fields use `type: 'model-select'` with a **↺ Load** button that calls `api.fetchAiModels({ provider, apiKey, host, ... })` to fetch available models from each provider's live API:
- Anthropic: hardcoded list (no public models API)
- OpenAI: GET `/v1/models` → filter `gpt-*`, `o1-*`, `o3-*`
- Gemini: GET `/v1beta/models?key={key}` → filter `generateContent`-capable
- OpenRouter: GET `/api/v1/models` → all
- Azure: GET `{endpoint}/openai/deployments?api-version={version}` → list deployments

### `loadFrom` vs `providers`
Fields with `providers: [...]` are **hidden** when the active provider doesn't match (used in AI PROVIDER section for show/hide of provider-specific fields). Fields with `loadFrom: 'gemini'` etc. tell the Load button which provider API to call — these are **always visible** (each provider has its own section).

### IPC: `fetch-ai-models`
`main.js` IPC handler `fetch-ai-models` uses Node `fetch()` (available in Electron 33 / Node 20) to call provider model APIs from the main process. Returns `{ models: [{id, name}] }` or `{ error }`.

---

## Data Flow Diagram

```text
main.py / Program.cs (main loop)
├── [startup] estimator.ValidateApiKeyAsync()
│   └── ping each configured provider (max_tokens=1)
│
├── scanner.get_market_prices(token_ids) → CLOB /midpoint
├── [ghost check] clobClient.GetConditionalBalanceAsync(token_id) → CLOB /balance-allowance
├── scanner.check_market_resolution(condition_id) → CLOB /markets/{id}
├── scanner.scan() → Gamma API /events → filtered list[MarketInfo]
│
├── estimator.estimate(market)
│   ├── single: N calls to one provider → trimmed mean
│   └── multi:  ceil(N/P) calls each → score → trimmed mean of provider means
│
├── portfolio.generate_signal() → Kelly criterion → Signal
├── portfolio.check_risk() → 5 layers + cooldown → bool
├── trader.execute() → CLOB /order (GTC) + poll
│
├── persistence.save_snapshot() → data/portfolio.json (atomic)
└── persistence.append_trade() → data/trades.jsonl
```

---

## File Index

### Python

| File | Purpose |
|------|---------|
| [python/main.py](python/main.py) | Orchestration loop |
| [python/config.py](python/config.py) | BotConfig — per-provider fields, backward compat |
| [python/models.py](python/models.py) | Domain dataclasses |
| [python/market_scanner.py](python/market_scanner.py) | Gamma API + spread filter + batch prices |
| [python/estimator.py](python/estimator.py) | Multi-provider AI ensemble, scoring, validation |
| [python/portfolio.py](python/portfolio.py) | Kelly sizing, risk checks, cooldown, ghost removal |
| [python/trader.py](python/trader.py) | PaperTrader + LiveTrader + ghost detection |
| [python/persistence.py](python/persistence.py) | Atomic JSON portfolio + JSONL trades |
| [python/notifier.py](python/notifier.py) | HTML email notifications (8 event types) |
| [python/logger_setup.py](python/logger_setup.py) | Colored console + JSON file logger |

### .NET

| File | Purpose |
|------|---------|
| [dotnet/PolymarketBot/Program.cs](dotnet/PolymarketBot/Program.cs) | Async main loop |
| [dotnet/PolymarketBot/BotConfig.cs](dotnet/PolymarketBot/BotConfig.cs) | Config — per-provider fields, backward compat |
| [dotnet/PolymarketBot/Services/Estimator.cs](dotnet/PolymarketBot/Services/Estimator.cs) | Multi-provider AI ensemble, scoring, validation |
| [dotnet/PolymarketBot/Services/MarketScanner.cs](dotnet/PolymarketBot/Services/MarketScanner.cs) | Gamma API + spread filter |
| [dotnet/PolymarketBot/Services/Portfolio.cs](dotnet/PolymarketBot/Services/Portfolio.cs) | Kelly sizing, risk, cooldown |
| [dotnet/PolymarketBot/Services/LiveTrader.cs](dotnet/PolymarketBot/Services/LiveTrader.cs) | Live CLOB + ghost detection |
| [dotnet/PolymarketBot/Services/PaperTrader.cs](dotnet/PolymarketBot/Services/PaperTrader.cs) | Simulated execution |
| [dotnet/PolymarketBot/Services/ClobApiClient.cs](dotnet/PolymarketBot/Services/ClobApiClient.cs) | EIP-712 + HMAC + auto-claim |
| [dotnet/PolymarketBot/Services/Notifier.cs](dotnet/PolymarketBot/Services/Notifier.cs) | HTML email notifications |
| [dotnet/PolymarketBot/Services/PersistenceService.cs](dotnet/PolymarketBot/Services/PersistenceService.cs) | Atomic JSON + JSONL |

---

## Common Gotchas

1. **Both implementations must stay in sync** — any logic change in Python → mirror in .NET.
2. **Gamma API JSON quirk** — `outcomes`, `outcomePrices`, `clobTokenIds` can be JSON-encoded strings or arrays.
3. **CLOB order amounts**: BUY `amount` = USD; SELL `amount` = tokens.
4. **Portfolio value vs bankroll**: risk limits use `bankroll + total_exposure()`. Never confuse.
5. **Bankroll can go negative** — normal with open positions + API costs. Only fatal when `bankroll + total_exposure < $1`.
6. **`fair_estimate_at_entry`** stored per position; updated by re-estimation during review.
7. **CLOB minimum = 5 tokens** — TopupAndSell path for positions under 5 tokens.
8. **CLOB min pre-check** uses `price + 0.02` (aggressive price after BUY tick adjustment), not raw market price.
9. **Tick size** — CLOB API may return Number or String JSON; always handle both.
10. **Cooldown is in-memory** — not persisted, resets on restart.
11. **Ghost check every cycle** in live mode only. Positions with < 0.1 tokens on-chain are written off.
12. **Multi-provider validation**: stops only if ALL configured providers fail. Partial failure = warn + continue.
13. **Provider model fields are independent** — `anthropic_model`, `gemini_model`, etc. do not fall back to each other. Hardcoded defaults: Anthropic=`claude-sonnet-4-6`, OpenAI=`gpt-4o`, Gemini=`gemini-2.0-flash`.
14. **Anthropic SDK TextBlock**: `response.content[0]` can be ThinkingBlock, ToolUseBlock, etc. Always use `next(b for b in response.content if hasattr(b, "text"), None)`.
15. **No legacy `claude_model`/`ai_model` fields** — reading them from JSON still works (backward compat via `from_env()` fallback chain) but don't use them in new configs.
