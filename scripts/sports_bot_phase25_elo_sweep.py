"""Phase 2.5 — Elo-only parameter sweep on 2024 (excluding 30-day burn-in),
then ONE held-out 2025 backtest with the chosen params.

GUARDRAILS (all enforced by structure):
  - Sweep grid: K in {2,3,4,6,8,10}, HFA in {15,20,24,30,35}, MoV in {True, False}.
    Total 60 combinations.
  - Objective: 2024 Brier, excluding games before day 31 (cold-start burn-in,
    since 2024 was seeded flat at 1500 with no 2023 in cache).
  - 2025 is NEVER touched during selection — strictly held-out.
  - After selecting best (K, HFA, MoV) by burn-in-corrected 2024 Brier:
      1. Re-report 2024 Brier WITHOUT burn-in correction to check sensitivity.
      2. Re-run on 2025 ONCE with chosen params. Report Brier, log-loss, acc,
         5-bin calibration, spread sample with the as-of ratings used.
  - 0.003 Brier movement is reported as "within noise" honestly.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import numpy as np
from sports_research.mlb import cache, elo


# Sweep grid
K_GRID = [2, 3, 4, 6, 8, 10]
HFA_GRID = [15, 20, 24, 30, 35]
MOV_GRID = [True, False]

# Burn-in cutoff: 2024 opening day was March 28 (regular MLB schedule).
# First 30 days = through April 26. Use 31 days = April 27 as the start of
# the tuning objective window. The "before this date" games still run to
# update Elo, they just don't count for the Brier objective.
BURN_IN_START_DATE_2024 = "2024-04-27"


def walk_forward_predictions(con, season: int, prior_season: int,
                             k: float, hfa: float, mov_enabled: bool
                             ) -> list[tuple[str, float, int]]:
    """Walk-forward over a season with the given Elo params. Returns list of
    (game_date, predicted_home_win_p, actual_home_win). Predict-then-update
    enforced; this is the canonical walk-forward loop."""
    # Seed
    seed_rows = list(con.execute(
        "SELECT team_id, rating FROM elo_ratings WHERE season = ?", (prior_season,)
    ))
    if len(seed_rows) == 30:
        ratings = {r["team_id"]: elo.regress_to_mean(r["rating"]) for r in seed_rows}
    elif len(seed_rows) == 0:
        team_rows = list(con.execute(
            "SELECT DISTINCT home_team_id AS tid FROM games WHERE season = ?",
            (season,),
        ))
        ratings = {r["tid"]: elo.BASE_RATING for r in team_rows}
    else:
        raise RuntimeError(f"Unexpected partial seed: {len(seed_rows)} rows in {prior_season} Elo")

    games = list(con.execute(
        """SELECT game_pk, game_date, home_team_id, home_team_name,
                  away_team_id, away_team_name, home_score, away_score
           FROM games
           WHERE season = ? AND game_type = 'R'
                 AND home_score IS NOT NULL AND away_score IS NOT NULL
                 AND home_score != away_score
           ORDER BY game_date, game_pk""",
        (season,),
    ))

    out = []
    for g in games:
        h_id, a_id = g["home_team_id"], g["away_team_id"]
        if h_id not in ratings or a_id not in ratings:
            continue
        h_rating = ratings[h_id]
        a_rating = ratings[a_id]
        p = elo.expected_win_probability(h_rating, a_rating, hfa=hfa)
        actual = int(g["home_score"] > g["away_score"])
        out.append((g["game_date"], p, actual, g["game_pk"], h_id, a_id,
                    g["home_team_name"], g["away_team_name"], h_rating, a_rating))

        h_state = elo.EloState(team_id=h_id, rating=h_rating, games_played=0,
                               last_updated_game_pk=None, season=season)
        a_state = elo.EloState(team_id=a_id, rating=a_rating, games_played=0,
                               last_updated_game_pk=None, season=season)
        new_h, new_a = elo.update_after_game(h_state, a_state, g["home_score"],
                                              g["away_score"], g["game_pk"],
                                              k=k, hfa=hfa, mov_enabled=mov_enabled)
        ratings[h_id] = new_h.rating
        ratings[a_id] = new_a.rating

    return out


def brier(preds, actuals):
    return float(np.mean((np.array(preds) - np.array(actuals)) ** 2))


def log_loss(preds, actuals):
    p = np.clip(preds, 1e-15, 1 - 1e-15)
    a = np.array(actuals)
    return float(-np.mean(a * np.log(p) + (1 - a) * np.log(1 - p)))


def accuracy(preds, actuals):
    p = np.array(preds) >= 0.5
    a = np.array(actuals) == 1
    return float(np.mean(p == a))


def calibration_table(preds, actuals):
    bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.000001)]
    rows = []
    preds = np.array(preds); actuals = np.array(actuals)
    for lo, hi in bins:
        mask = (preds >= lo) & (preds < hi)
        n = int(mask.sum())
        if n == 0:
            rows.append((f"[{lo:.1f}, {hi if hi<1.0 else 1.0:.1f})", 0, 0.0, 0.0))
        else:
            rows.append((f"[{lo:.1f}, {hi if hi<1.0 else 1.0:.1f})",
                         n, float(preds[mask].mean()), float(actuals[mask].mean())))
    return rows


def main():
    con = cache.open_db()
    print("=" * 80)
    print(" Phase 2.5 — Elo parameter sweep on 2024, held-out 2025")
    print("=" * 80)
    print(f"\n  Sweep grid: K x HFA x MoV  =  {len(K_GRID)} x {len(HFA_GRID)} x {len(MOV_GRID)} = "
          f"{len(K_GRID)*len(HFA_GRID)*len(MOV_GRID)} combos")
    print(f"  Burn-in: games before {BURN_IN_START_DATE_2024} excluded from 2024 objective")
    print(f"  Held-out test: 2025 regular season (NEVER touched during sweep)")
    print(f"  Baseline to beat: 0.2429 (Phase 2 Elo-only with K=4, HFA=24, MoV on)")

    # =========================
    # STEP 1: Sweep on 2024 with burn-in correction
    # =========================
    print("\n[STEP 1] Sweeping 2024 (burn-in-corrected)...")
    results = []
    for k in K_GRID:
        for hfa in HFA_GRID:
            for mov in MOV_GRID:
                preds_2024 = walk_forward_predictions(con, 2024, 2023, k, hfa, mov)
                bp_full = [p for _, p, _, *_ in preds_2024]
                ba_full = [a for _, _, a, *_ in preds_2024]
                bp_burnin = [p for d, p, _, *_ in preds_2024 if d >= BURN_IN_START_DATE_2024]
                ba_burnin = [a for d, _, a, *_ in preds_2024 if d >= BURN_IN_START_DATE_2024]
                br_full = brier(bp_full, ba_full)
                br_burnin = brier(bp_burnin, ba_burnin)
                results.append({
                    "k": k, "hfa": hfa, "mov": mov,
                    "brier_burnin": br_burnin,
                    "brier_full": br_full,
                    "n_burnin": len(bp_burnin),
                    "n_full": len(bp_full),
                })

    # Print full sweep table (sorted by burn-in Brier)
    results.sort(key=lambda r: r["brier_burnin"])
    print(f"\n  Top 10 by burn-in Brier (n_burnin ≈ {results[0]['n_burnin']} games):")
    print(f"  {'K':>3s} {'HFA':>4s} {'MoV':>5s}  {'Brier (burn-in)':>16s} {'Brier (full)':>14s}")
    for r in results[:10]:
        print(f"  {r['k']:>3d} {r['hfa']:>4d} {str(r['mov']):>5s}  "
              f"{r['brier_burnin']:>16.6f} {r['brier_full']:>14.6f}")
    print(f"\n  Bottom 5 by burn-in Brier (for context):")
    for r in results[-5:]:
        print(f"  {r['k']:>3d} {r['hfa']:>4d} {str(r['mov']):>5s}  "
              f"{r['brier_burnin']:>16.6f} {r['brier_full']:>14.6f}")

    best = results[0]
    print(f"\n  Selected: K={best['k']}, HFA={best['hfa']}, MoV={best['mov']}  "
          f"(burn-in Brier {best['brier_burnin']:.6f})")

    # =========================
    # STEP 2: Burn-in sensitivity check
    # =========================
    print(f"\n[STEP 2] Burn-in sensitivity check: top-3 by full Brier vs top-3 by burn-in Brier")
    full_sorted = sorted(results, key=lambda r: r["brier_full"])
    burnin_sorted = results  # already sorted by burnin
    print(f"  {'Rank':>4s}  {'by burn-in':>30s}  {'by full':>30s}")
    for i in range(3):
        r1 = burnin_sorted[i]
        r2 = full_sorted[i]
        s1 = f"K={r1['k']:>2d} HFA={r1['hfa']:>2d} MoV={str(r1['mov'])[0]} -> {r1['brier_burnin']:.5f}"
        s2 = f"K={r2['k']:>2d} HFA={r2['hfa']:>2d} MoV={str(r2['mov'])[0]} -> {r2['brier_full']:.5f}"
        print(f"  {i+1:>4d}  {s1:>30s}  {s2:>30s}")
    same_top = (full_sorted[0]['k'] == burnin_sorted[0]['k']
                and full_sorted[0]['hfa'] == burnin_sorted[0]['hfa']
                and full_sorted[0]['mov'] == burnin_sorted[0]['mov'])
    if same_top:
        print(f"  -> Burn-in vs full-2024 chose the SAME best params. Burn-in was non-decisive.")
    else:
        print(f"  -> Burn-in vs full chose DIFFERENT best params. Burn-in was load-bearing.")
        print(f"     Going with burn-in choice (operator's explicit guidance).")

    # =========================
    # STEP 3: Held-out 2025 backtest with chosen params
    # =========================
    print(f"\n[STEP 3] Held-out 2025 backtest with K={best['k']}, HFA={best['hfa']}, MoV={best['mov']}")
    preds_2025 = walk_forward_predictions(con, 2025, 2024,
                                          best['k'], best['hfa'], best['mov'])
    pp = [p for _, p, _, *_ in preds_2025]
    aa = [a for _, _, a, *_ in preds_2025]

    # Also compute Elo-only baseline (Phase 2 config: K=4, HFA=24, MoV on) on 2025 for direct compare
    preds_2025_baseline = walk_forward_predictions(con, 2025, 2024, 4, 24, True)
    bp = [p for _, p, _, *_ in preds_2025_baseline]
    ba = [a for _, _, a, *_ in preds_2025_baseline]

    print(f"\n[STEP 4] Spread sample of 20 2025 predictions with chosen params")
    print(f"  {'date':<11s} {'home (rating)':<28s} {'away (rating)':<28s}  {'p_chosen':>9s} {'actual':>6s}")
    n = len(preds_2025)
    idxs = sorted(set([
        0, 1, 2, 3,
        int(n * 0.10), int(n * 0.20), int(n * 0.30), int(n * 0.40),
        int(n * 0.50), int(n * 0.60), int(n * 0.70), int(n * 0.80),
        int(n * 0.85), int(n * 0.90), int(n * 0.95),
        n - 5, n - 4, n - 3, n - 2, n - 1,
    ]))[:20]
    for i in idxs:
        d, p, act, gpk, hid, aid, hn, an, hr, ar = preds_2025[i]
        print(f"  {d:<11s} {hn + f' ({hr:.0f})':<28s} {an + f' ({ar:.0f})':<28s}  {p:>9.3f} {act:>6d}")

    # =========================
    # STEP 5: Metrics + verdict
    # =========================
    print(f"\n[STEP 5] 2025 HELD-OUT metrics")
    print(f"  {'Model':<32s} {'Brier':>9s} {'LogLoss':>9s} {'Acc':>8s}")
    print(f"  {'Phase 2 Elo (K=4,HFA=24,MoV=T)':<32s} {brier(bp,ba):>9.4f} {log_loss(bp,ba):>9.4f} {accuracy(bp,ba):>8.4f}")
    print(f"  {'Phase 2.5 chosen (K=' + str(best['k']) + ',HFA=' + str(best['hfa']) + ',MoV=' + str(best['mov'])[0] + ')':<32s} "
          f"{brier(pp,aa):>9.4f} {log_loss(pp,aa):>9.4f} {accuracy(pp,aa):>8.4f}")

    print(f"\n[STEP 6] 5-bin calibration — Phase 2.5 chosen on held-out 2025")
    print(f"  {'bin':<14s} {'n':>5s} {'mean_pred':>11s} {'observed':>10s} {'gap':>8s}")
    for label, n_bin, mp, obs in calibration_table(pp, aa):
        gap = obs - mp if n_bin > 0 else 0.0
        print(f"  {label:<14s} {n_bin:>5d} {mp:>11.4f} {obs:>10.4f} {gap:>+8.4f}")

    # =========================
    # STEP 7: Verdict
    # =========================
    final_brier = brier(pp, aa)
    elo_prior = brier(bp, ba)
    print("\n" + "=" * 80)
    print(" VERDICT (held-out 2025)")
    print(f"  Phase 2.5 chosen Brier:        {final_brier:.4f}")
    print(f"  Phase 2 Elo-only prior Brier:  {elo_prior:.4f}")
    print(f"  Dossier gate:                  0.2200")
    diff_vs_prior = final_brier - elo_prior
    print(f"  Move vs Phase-2 prior: {diff_vs_prior:+.4f}", end="")
    if abs(diff_vs_prior) < 0.003:
        print("  (within noise — operator threshold)")
    elif diff_vs_prior < 0:
        print("  (improvement)")
    else:
        print("  (degradation)")
    if final_brier <= 0.22:
        print(f"  vs DOSSIER GATE: PASSED")
    else:
        print(f"  vs DOSSIER GATE: FAILED ({final_brier - 0.22:+.4f})")
    print("=" * 80)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
