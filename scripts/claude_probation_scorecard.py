#!/usr/bin/env python3
"""Claude-bot probation scorecard (READ-ONLY on bot data).

Records a probation baseline and reports the exit-reason / loss breakdown.
Never writes bot state — the only thing it writes is its own CSV under
~/.local/state/trooth/claude_probation.csv.

Per-trade realized P&L is DERIVED, because trade records carry no pnl field.
For a SELL/exit record the share-purchase identity holds:

    pnl = shares * exit_price - size_usd          (size_usd = cost basis)

verified on a known row (Starmer stop_loss: 24.12*0.465 - 17.97 = -6.75) and
reconciled in-script against portfolio.json total_realized_pnl. The formula is
uniform across every exit type — take-profit, stop_loss, resolved_won/lost
(price 1.0/0.0), resolved_void (price 0.5), ghost (price 0.0), operator_close.

Everything is guarded: missing fields/files degrade gracefully, never crash.
"""
import csv
import datetime as dt
import json
import os
import sys
from collections import defaultdict, deque

# Repo-root-relative so it works regardless of CWD; CSV in user state dir.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTFOLIO = os.path.join(_REPO, "data", "portfolio.json")
TRADES = os.path.join(_REPO, "data", "trades.jsonl")
CSV_PATH = os.path.expanduser("~/.local/state/trooth/claude_probation.csv")

FIELDS = ["date", "realized", "api_cost", "closed_count", "window_realized",
          "window_api_cost", "net_of_cost", "in_window_closed",
          "avg_hold_days_window", "window_first_stop_avg",
          "window_reentry_stop_pnl", "note"]


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_portfolio():
    try:
        d = json.load(open(PORTFOLIO))
        return _f(d.get("total_realized_pnl")), _f(d.get("total_api_cost"))
    except Exception as e:
        print(f"WARN: portfolio load failed: {e}", file=sys.stderr)
        return 0.0, 0.0


def load_trades():
    rows = []
    try:
        with open(TRADES) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"WARN: trades load failed: {e}", file=sys.stderr)
    return rows


def sell_pnl(r):
    """Realized P&L for one SELL/exit record (see module docstring)."""
    return _f(r.get("shares")) * _f(r.get("price")) - _f(r.get("size_usd"))


def compute_hold_days(rows):
    """FIFO-pair BUY -> SELL per condition_id. Returns {sell_trade_id: hold_days}.

    Handles re-entries and partial closes: each SELL consumes shares from the
    oldest open BUY(s); hold time is the share-weighted mean. A SELL with no
    matching prior BUY (legacy/imported) is simply left undetermined.
    """
    open_buys = defaultdict(deque)
    hold = {}
    for r in sorted(rows, key=lambda x: _f(x.get("timestamp"))):
        cid = r.get("condition_id")
        act = r.get("action")
        ts = _f(r.get("timestamp"))
        sh = _f(r.get("shares"))
        if act == "BUY":
            open_buys[cid].append([ts, sh])
        elif act == "SELL":
            remaining, w_sum, w_tot = sh, 0.0, 0.0
            while remaining > 1e-9 and open_buys[cid]:
                b_ts, b_sh = open_buys[cid][0]
                take = min(remaining, b_sh)
                w_sum += take * (ts - b_ts)
                w_tot += take
                remaining -= take
                if b_sh - take <= 1e-9:
                    open_buys[cid].popleft()
                else:
                    open_buys[cid][0][1] = b_sh - take
            if w_tot > 0:
                hold[r.get("trade_id")] = (w_sum / w_tot) / 86400.0
    return hold


def breakdown(recs):
    """exit_reason -> (count, total_pnl, avg_pnl), sorted by total_pnl ascending."""
    agg = defaultdict(lambda: [0, 0.0])
    for r in recs:
        er = r.get("exit_reason") or "(unspecified)"
        agg[er][0] += 1
        agg[er][1] += sell_pnl(r)
    out = [(er, c, tot, (tot / c if c else 0.0)) for er, (c, tot) in agg.items()]
    out.sort(key=lambda x: x[2])
    return out


def _print_table(title, recs):
    print(title)
    print(f"  {'exit_reason':<26}{'count':>6}{'total_pnl':>13}{'avg_pnl':>11}")
    print("  " + "-" * 56)
    for er, c, tot, avg in breakdown(recs):
        print(f"  {er:<26}{c:>6}{tot:>+13.2f}{avg:>+11.2f}")
    print("  " + "-" * 56)
    tc = len(recs)
    tt = sum(sell_pnl(r) for r in recs)
    print(f"  {'TOTAL':<26}{tc:>6}{tt:>+13.2f}{(tt / tc if tc else 0.0):>+11.2f}")


def main():
    realized, api_cost = load_portfolio()
    rows = load_trades()
    sells = [r for r in rows if r.get("action") == "SELL"]
    hold = compute_hold_days(rows)
    closed_count = len(sells)
    computed_sum = sum(sell_pnl(r) for r in sells)

    # Lifetime stop ordinal per condition_id: a stop's ordinal is 1 the first
    # time that market is stopped (ever), 2 on the next stop, etc. This drives
    # the two fix-signature metrics — a re-entry stop (ordinal >= 2) is exactly
    # what the circuit breaker prevents; a first stop (ordinal == 1) is the
    # severity the protective inner-review targets.
    stop_records = sorted(
        (r for r in sells if r.get("exit_reason") == "stop_loss"),
        key=lambda r: _f(r.get("timestamp")),
    )
    stop_ordinal = {}
    _seen = defaultdict(int)
    for r in stop_records:
        _seen[r.get("condition_id")] += 1
        stop_ordinal[r.get("trade_id")] = _seen[r.get("condition_id")]

    # ---- read existing baseline (first row), if any ----
    have_baseline = os.path.exists(CSV_PATH) and os.path.getsize(CSV_PATH) > 0
    baseline = None
    if have_baseline:
        try:
            with open(CSV_PATH) as fh:
                existing = list(csv.DictReader(fh))
            baseline = existing[0] if existing else None
            have_baseline = baseline is not None
        except Exception:
            have_baseline = False

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    except Exception as e:
        print(f"WARN: could not create CSV dir: {e}", file=sys.stderr)

    base_date = ""
    base_realized = base_api = 0.0
    in_window = []
    win_first_stop_avg = None
    win_reentry_pnl = None

    if not have_baseline:
        try:
            with open(CSV_PATH, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                w.writeheader()
                w.writerow({"date": now, "realized": round(realized, 4),
                            "api_cost": round(api_cost, 4), "closed_count": closed_count,
                            "window_realized": 0, "window_api_cost": 0, "net_of_cost": 0,
                            "in_window_closed": 0, "avg_hold_days_window": 0,
                            "window_first_stop_avg": 0, "window_reentry_stop_pnl": 0,
                            "note": "BASELINE"})
            wrote = "BASELINE row written"
        except Exception as e:
            wrote = f"CSV write FAILED: {e}"
    else:
        base_date = baseline.get("date", "")
        base_realized = _f(baseline.get("realized"))
        base_api = _f(baseline.get("api_cost"))
        try:
            b_ts = dt.datetime.fromisoformat(base_date).timestamp()
        except Exception:
            b_ts = None
        if b_ts is not None:
            in_window = [r for r in sells if _f(r.get("timestamp")) > b_ts]
        win_real = realized - base_realized
        win_api = api_cost - base_api
        net = win_real - win_api
        hds = [hold[r["trade_id"]] for r in in_window if r.get("trade_id") in hold]
        avg_hold = (sum(hds) / len(hds)) if hds else 0.0

        # Fix-signature metrics over stops that occurred IN the window.
        if b_ts is not None:
            win_stops = [r for r in stop_records if _f(r.get("timestamp")) > b_ts]
        else:
            win_stops = []
        first_stops = [r for r in win_stops if stop_ordinal.get(r.get("trade_id")) == 1]
        reentry_stops = [r for r in win_stops if stop_ordinal.get(r.get("trade_id"), 1) >= 2]
        # overshoot-fix signal: mean severity of first-ever stops this window
        win_first_stop_avg = (sum(sell_pnl(r) for r in first_stops) / len(first_stops)
                              if first_stops else 0.0)
        # breaker signal: total bleed from re-entry stops this window (-> should approach 0)
        win_reentry_pnl = sum(sell_pnl(r) for r in reentry_stops)

        try:
            with open(CSV_PATH, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                w.writerow({"date": now, "realized": round(realized, 4),
                            "api_cost": round(api_cost, 4), "closed_count": closed_count,
                            "window_realized": round(win_real, 4),
                            "window_api_cost": round(win_api, 4), "net_of_cost": round(net, 4),
                            "in_window_closed": len(in_window),
                            "avg_hold_days_window": round(avg_hold, 2),
                            "window_first_stop_avg": round(win_first_stop_avg, 2),
                            "window_reentry_stop_pnl": round(win_reentry_pnl, 2),
                            "note": ""})
            wrote = "window row appended"
        except Exception as e:
            wrote = f"CSV append FAILED: {e}"

    # ---- (a) probation summary ----
    print("=" * 64)
    print("CLAUDE-BOT PROBATION SCORECARD")
    print("=" * 64)
    print(f"  as of:           {now}")
    print(f"  realized P&L:    ${realized:+.2f}")
    print(f"  API cost:        ${api_cost:.2f}")
    print(f"  net of cost:     ${realized - api_cost:+.2f}")
    print(f"  closed trades:   {closed_count}")
    print(f"  derived Sum pnl: ${computed_sum:+.2f}  "
          f"(reconcile vs realized, delta=${computed_sum - realized:+.2f})")
    print(f"  CSV:             {CSV_PATH}")
    print(f"  CSV action:      {wrote}")
    if have_baseline:
        print(f"  --- window vs baseline {base_date} ---")
        print(f"  window realized: ${realized - base_realized:+.2f}")
        print(f"  window API cost: ${api_cost - base_api:.2f}")
        print(f"  window net:      ${(realized - base_realized) - (api_cost - base_api):+.2f}")
        print(f"  in-window closes:{len(in_window)}")
        if win_first_stop_avg is not None:
            print(f"  first-stop avg (overshoot fix):    ${win_first_stop_avg:+.2f}")
            print(f"  re-entry stop pnl (breaker fix):   ${win_reentry_pnl:+.2f}")

    # ---- (b) exit-reason breakdown ----
    print()
    _print_table("EXIT-REASON BREAKDOWN (lifetime, biggest bleeders on top)", sells)
    if have_baseline and in_window:
        print()
        _print_table(f"EXIT-REASON BREAKDOWN (window since {base_date})", in_window)


if __name__ == "__main__":
    main()
