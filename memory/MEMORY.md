# Polymarket Bot — Session Memory

## Key Reference

Full codebase docs: `llm.md` (architecture), `memory.md` (current state + feature notes). Config template: `polymarket_bot_config.json.example`.

## Project Structure

- Two identical implementations: **Python** (`python/`) and **.NET 8** (`dotnet/PolymarketBot/`)
- Both read `polymarket_bot_config.json` at project root (gitignored)
- Both write to `data/`: `portfolio.json`, `trades.jsonl`, `bot.log`
- Config priority: CLI arg → env var → config file → code default
- **Dashboard** (`dashboard/`) — Electron desktop app, launched via `run-dashboard.bat` (no terminal) or `run-dashboard.vbs`

## AI Provider System

Multi-provider support: Anthropic, OpenAI, Gemini, OpenRouter, Azure OpenAI.

**Config** — each provider has key/host/model + enabled flag:

```json
"ai_provider": "anthropic",
"multi_provider": true,
"anthropic_enabled": true,   "anthropic_model": "claude-sonnet-4-6",
"gemini_enabled": false,     "gemini_model": "gemini-2.0-flash",
"openai_enabled": false,     "openai_model": "gpt-4o",
"openrouter_enabled": false,
"azure_openai_enabled": true, "azure_openai_deployment": "gpt-4o-mini"
```

**Provider included only if:** `*_enabled: true` AND api_key non-empty AND (azure: endpoint+deployment set).

**Removed fields** (legacy): `claude_model`, `ai_model` — still read for backward compat.

**Multi-provider scoring:**
```
conviction = |provider_mean - market_price|
confidence = 1 / (std_dev + 0.01)
score      = conviction × confidence
```
Winner `⭐` logged; final = trimmed mean of per-provider means. Stops only if ALL fail.

**Rate-limit cooldown (.NET):** provider exhausts 429 retries → added to `_rateLimitedThisCycle`, skipped for rest of cycle. `estimator.ResetCycle()` at start of each cycle.

## Critical Architecture Points

- **CLOB minimum = 5 tokens** per order. Tiny positions use TopupAndSell.
- **Bankroll ≠ portfolio value**: `portfolio_value = bankroll + total_exposure()`. Risk limits use portfolio value.
- **Bankroll can go negative** — halts only when `bankroll + total_exposure < $1`.
- **CLOB min pre-check** uses `price + 0.02` (aggressive price), not raw market price.
- **Tick size** — CLOB `/tick-size` can return `Number` or `String` JSON; handle both.
- **fair_estimate_at_entry** stored per Position — used for edge-gone; updated by mid-cycle re-estimation.
- **Gamma API quirk**: `outcomes`, `outcomePrices`, `clobTokenIds` may be JSON-encoded strings inside JSON.
- **Anthropic TextBlock**: use `next(b for b in response.content if hasattr(b, "text"), None)`.
- **.NET Estimator** uses raw HttpClient for all providers. Python uses `anthropic` SDK + `requests`.
- **ParseProviderResponse (.NET)** takes `provider` string param — fixed bug where it always used `_config.AiProvider` (parsed azure_openai as Anthropic format → KeyNotFoundException).
- **Auto-claim (.NET only)**: won positions trigger on-chain EIP-155 tx to Polygon CTF.redeemPositions.

## All Implemented Features

- **Per-provider enable/disable** — `*_enabled` flags in config; dashboard UI has toggle switches per provider
- **Multi-provider estimation** — score/aggregate across all configured+enabled providers
- **API key validation** — startup validates all enabled providers; multi mode stops only if all fail
- **Provider rate-limit cooldown** (.NET) — 429 exhaustion skips provider for rest of cycle
- **Ghost position detection** — on-chain balance < 0.1 tokens → write off with `exit_reason="ghost"`
- **Position cooldown** — 2 cycles after any close before re-entering same market
- **Re-estimation during review** — price moved >10% → re-run AI to refresh fair value
- **Confidence filter** — `max_estimate_std`: skip if ensemble std dev too high
- **Spread filter** — `max_spread`: skip wide bid-ask markets
- **HTML emails** — color-coded per event; startup email has 4 sections (Portfolio/AI/Risk/Scan)
- **Config dump at startup** — logs 4 sections: `── AI ──`, `── RISK ──`, `── SCAN ──`, `── EXITS ──`
- **Log copy button** — `⎘ copy` in log controls; copies to clipboard with 1.5s ✓ feedback
- **Dashboard settings persistence** — `dashboard-settings.json` in bot root (replaces localStorage). IPC: `read-settings`/`write-settings`. Persists: lang, theme, bot-mode, verbose, console, panel-left-w, panel-upper-h.
- **Panel size persistence** — `dragResize` saves raw px values on mouseup via `onDone` callback
- **No-terminal launch** — `run-dashboard.bat` uses `start ""` to detach; `run-dashboard.vbs` for truly hidden
- **Dashboard icon** — `setup-icon.js` generates `icon.png` (256×256, Polymarket blue). Run once: `node setup-icon.js`
- **SELL order fixes** — Floor not Round, -2 ticks aggression, balance-allowance plural key

## Azure OpenAI Notes

`azure_openai_deployment` must be set — it's both the deployment name and model identifier. Example: `"azure_openai_deployment": "gpt-4o-mini"`. Without it, provider is excluded from GetConfiguredProviders().

## Dashboard Key Patterns

- **Config editor**: per-provider sections with Key + Host + Model + enabled toggle. `loadFrom` tells Load button which API to call.
- **Settings**: `dashboard-settings.json` in bot root, loaded async at boot before initTheme/initLang/setupResize
- **Bot spawn**: `shell: false` for `.exe`, `shell: true` for `python`/`dotnet run`
- **Log isolation**: `logClearedAt = Date.now()` on load; reset on bot start
- **Log rotation**: `bot.log` → `bot-TIMESTAMP.log` before each start
- **Timestamp normalization**: `parseTs(ts)` strips .NET's 7-decimal fractional seconds
- **Charts**: `animation: false`; `chart.update('none')` — no flicker
- **FileShare**: `new FileStream(..., FileShare.ReadWrite)` in Program.cs
- **Stale exe**: after .NET changes → `dotnet build -c Debug` from `dotnet/PolymarketBot/`
- **`t` variable shadowing**: `refresh()` must use `[p, tr, l]` not `[p, t, l]`
- **IPC channels**: `read-portfolio`, `read-trades`, `read-logs`, `read-config`, `write-config`, `get-data-dir`, `set-data-dir`, `browse-data-dir`, `bot-status`, `start-bot`, `stop-bot`, `save-file`, `open-logs-dir`, `fetch-ai-models`, `read-settings`, `write-settings`
- **Push events** (main→renderer): `file-changed`, `bot-output`, `bot-stopped`

## ⚠️ Temporary Config Overrides (revert when bankroll replenished)

- [project_risk_params_temp.md](project_risk_params_temp.md) — kelly_fraction=1.0, max_position_pct=0.30 (normal: 0.50 / 0.15)

## Both Implementations Must Stay In Sync

Any logic change in Python → mirror in .NET and vice versa.
