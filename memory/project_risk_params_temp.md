---
name: Temporary risk parameter overrides (low bankroll)
description: kelly_fraction and max_position_pct were raised temporarily because bankroll is too low to meet CLOB minimums with normal Kelly sizing. Revert when bankroll is replenished.
type: project
---

As of 2026-03-25, risk parameters were changed due to small bankroll (~$6.50 free, ~$12 total portfolio):

**Current (temporary) values:**
- `kelly_fraction`: 1.0 (full Kelly — aggressive)
- `max_position_pct`: 0.30

**Normal (target) values to restore:**
- `kelly_fraction`: 0.50 (half Kelly)
- `max_position_pct`: 0.15

**Why:** Portfolio of ~$12 can't meet CLOB minimum (5 tokens) with half-Kelly sizing. Kelly formula: `kelly_fraction × (edge / (1 - market_price)) × portfolio`. With 0.50 Kelly and $12 portfolio, most trades produce $1.85–$2.03, but CLOB minimums are $3.65–$4.34.

**How to apply:** When Michael replenishes bankroll (target portfolio ~$25+), revert both values in `polymarket_bot_config.json`. Verify with one cycle that "TOO SMALL" messages disappear before declaring success.

**Note:** max_position_pct change from 15%→30% turned out irrelevant (Kelly itself was the constraint, not the cap). Still leaving at 30% as a small buffer.
