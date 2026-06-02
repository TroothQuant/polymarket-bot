"""Phase 2.5 — fit a starting-pitcher adjustment to Elo on 2024 ONLY, then
held-out backtest on 2025.

INTEGRITY GUARANTEES:
  - Pitcher skill at each game uses ONLY events with game_date < game_date
    (strict less-than). Enforced in pitcher_features.pitcher_skill_as_of().
  - Elo state at each game is the in-memory walk-forward state (predict-then-
    update), seeded from prior-season EOS regressed 75% to 1500.
  - Logistic regression alpha is fit on 2024 data only. 2025 is never seen
    during fitting.
  - The held-out 2025 backtest uses the fixed alpha from the 2024 fit. No
    re-tuning, no test-set leakage.

Method:
  1. Walk-forward over 2024 with the SAME logic as Phase 2 backtest, plus
     starting-pitcher skill computation for each game. Collect tuples
     (elo_log_odds, skill_delta, actual_home_win).
  2. Fit logistic regression: logit(home_win) ~ beta_elo*elo_log_odds + beta_skill*skill_delta
     Report betas + 2024 in-sample Brier improvement.
  3. Walk-forward over 2025 with the fitted model. Report Brier vs BOTH 0.22
     gate AND 0.2429 Elo-only prior. Spread sample with pitcher snapshots.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import numpy as np

from sports_research.mlb import cache, elo, pitcher_features as pf


def logit(p: float) -> float:
    p = max(1e-9, min(1 - 1e-9, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def fit_logistic(X: np.ndarray, y: np.ndarray, n_iter: int = 300, lr: float = 0.05,
                 l2: float = 1e-4) -> np.ndarray:
    """Hand-rolled logistic regression via gradient descent. Inputs:
       X: (n, k) feature matrix
       y: (n,) binary outcome 0/1
       Returns: (k,) coefficient vector. No intercept (Elo log-odds already
       carries the constant). L2 reg is tiny — prevents runaway when one
       feature has near-zero variance in 2024 (low risk here)."""
    n, k = X.shape
    beta = np.zeros(k)
    for _ in range(n_iter):
        scores = X @ beta
        # numerically stable sigmoid
        scores = np.clip(scores, -30, 30)
        p_hat = 1.0 / (1.0 + np.exp(-scores))
        grad = (X.T @ (p_hat - y)) / n + l2 * beta
        beta -= lr * grad
    return beta


def walk_forward_collect(con, season: int, prior_season: int) -> list[dict]:
    """Walk-forward over a season, collecting Elo + pitcher-skill features for
    each game. Returns list of dicts with the inputs each prediction used."""
    # Seed Elo from prior season EOS, regressed. If no prior season in cache
    # (e.g. 2024 walk-forward with no 2023 data), fall back to base 1500 for
    # all 30 MLB teams. This is documented as a known weakness for the 2024
    # fit's first ~30 games (early-season Elo is near-coin-flip), but the
    # PITCHER feature signal we're trying to measure is independent of Elo.
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
        if len(ratings) != 30:
            raise RuntimeError(
                f"Couldn't determine 30 teams for {season}; got {len(ratings)}"
            )
    else:
        raise RuntimeError(
            f"Unexpected partial seed: {len(seed_rows)} rows in {prior_season} Elo"
        )

    # League-average ERA for the target season (used as shrinkage target).
    # NOTE: this peeks at the season we're walking through — but only at an
    # aggregate constant, not at per-game outcomes — and it's pre-computed
    # once before the walk-forward begins. For training-time use this is
    # fine (we'd compute league avg from prior data in production); we'll
    # use the 2024-derived league avg for the 2025 held-out backtest below
    # to keep the held-out side fully walk-forward.
    league_era = pf.league_avg_era_for_season(con, season)

    games = list(con.execute(
        """SELECT g.game_pk, g.game_date, g.home_team_id, g.home_team_name,
                  g.away_team_id, g.away_team_name, g.home_score, g.away_score,
                  gsp.home_starter_id, gsp.away_starter_id
           FROM games g
           LEFT JOIN game_starting_pitchers gsp ON gsp.game_pk = g.game_pk
           WHERE g.season = ? AND g.game_type = 'R'
                 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                 AND g.home_score != g.away_score
           ORDER BY g.game_date, g.game_pk""",
        (season,),
    ))

    out = []
    for g in games:
        h_id, a_id = g["home_team_id"], g["away_team_id"]
        if h_id not in ratings or a_id not in ratings:
            continue
        h_rating = ratings[h_id]
        a_rating = ratings[a_id]
        p_elo = elo.expected_win_probability(h_rating, a_rating)

        home_skill = pf.pitcher_skill_as_of(con, g["home_starter_id"], g["game_date"], league_era)
        away_skill = pf.pitcher_skill_as_of(con, g["away_starter_id"], g["game_date"], league_era)
        skill_delta = home_skill - away_skill  # positive = home pitcher better than away

        actual = int(g["home_score"] > g["away_score"])

        out.append({
            "game_pk": g["game_pk"],
            "game_date": g["game_date"],
            "home_team_id": h_id, "home_team_name": g["home_team_name"],
            "away_team_id": a_id, "away_team_name": g["away_team_name"],
            "home_rating": h_rating, "away_rating": a_rating,
            "home_starter_id": g["home_starter_id"],
            "away_starter_id": g["away_starter_id"],
            "home_skill": home_skill,
            "away_skill": away_skill,
            "skill_delta": skill_delta,
            "p_elo": p_elo,
            "elo_log_odds": logit(p_elo),
            "actual_home_win": actual,
            "league_era_used": league_era,
        })

        # Update Elo state (predict-then-update)
        h_state = elo.EloState(team_id=h_id, rating=h_rating, games_played=0,
                               last_updated_game_pk=None, season=season)
        a_state = elo.EloState(team_id=a_id, rating=a_rating, games_played=0,
                               last_updated_game_pk=None, season=season)
        new_h, new_a = elo.update_after_game(h_state, a_state, g["home_score"],
                                              g["away_score"], g["game_pk"])
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

    # ============================
    # STEP 1: Walk-forward over 2024 and collect features for fitting
    # ============================
    print("=" * 78)
    print(" Phase 2.5 — fit pitcher adjustment on 2024, held-out backtest on 2025")
    print("=" * 78)

    print("\n[STEP 1] 2024 walk-forward (training data collection)")
    league_era_2024 = pf.league_avg_era_for_season(con, 2024)
    print(f"  League-avg ERA (starting pitchers) 2024: {league_era_2024:.3f}")
    train = walk_forward_collect(con, 2024, prior_season=2024 - 1)
    # NOTE: 2024 doesn't have a 2023 seed available in our cache; for the
    # 2024 walk-forward we seed everyone at 1500. That's a known weakness —
    # the FIRST few games of 2024 are nearly coin-flips on Elo. Fine for
    # fitting the pitcher coefficient because that's an additive feature
    # measured against actual outcomes.
    print(f"  Collected {len(train)} training rows from 2024 (Elo seed was 1500 across the board — "
          f"no 2023 data in cache; pitcher signal still measurable)")

    # Filter to rows that have valid starters (both sides)
    train_valid = [t for t in train if t["home_starter_id"] is not None
                                       and t["away_starter_id"] is not None]
    print(f"  {len(train_valid)} rows with both starters identified")

    # Build feature matrix
    X_train = np.array([[t["elo_log_odds"], t["skill_delta"]] for t in train_valid])
    y_train = np.array([t["actual_home_win"] for t in train_valid])
    print(f"  Feature stats (training):")
    print(f"    elo_log_odds: mean={X_train[:,0].mean():+.4f}, std={X_train[:,0].std():.4f}, "
          f"range=[{X_train[:,0].min():+.3f}, {X_train[:,0].max():+.3f}]")
    print(f"    skill_delta:  mean={X_train[:,1].mean():+.4f}, std={X_train[:,1].std():.4f}, "
          f"range=[{X_train[:,1].min():+.3f}, {X_train[:,1].max():+.3f}]")
    print(f"    home_win rate: {y_train.mean():.4f}")

    # ============================
    # STEP 2: Fit logistic regression on 2024
    # ============================
    print("\n[STEP 2] Fitting logistic regression on 2024 ONLY")
    beta = fit_logistic(X_train, y_train, n_iter=500, lr=0.1)
    beta_elo, beta_skill = float(beta[0]), float(beta[1])
    print(f"  beta_elo:   {beta_elo:+.4f}  (interpretation: if 1.0, Elo log-odds is already on the correct scale)")
    print(f"  beta_skill: {beta_skill:+.4f}  (interpretation: a 1-ERA-point pitcher advantage "
          f"contributes {beta_skill:+.4f} to logit)")

    # 2024 in-sample fit quality
    train_preds = np.array([sigmoid(beta_elo * t["elo_log_odds"] + beta_skill * t["skill_delta"])
                            for t in train_valid])
    train_preds_clamped = np.clip(train_preds, 0.05, 0.95)
    train_elo_only = np.array([sigmoid(t["elo_log_odds"]) for t in train_valid])
    print(f"  2024 IN-SAMPLE Brier: Elo+pitcher = {brier(train_preds_clamped, y_train):.4f}, "
          f"Elo-only = {brier(train_elo_only, y_train):.4f}")
    print(f"  2024 IN-SAMPLE LogLoss: Elo+pitcher = {log_loss(train_preds_clamped, y_train):.4f}, "
          f"Elo-only = {log_loss(train_elo_only, y_train):.4f}")
    print(f"  2024 IN-SAMPLE Accuracy: Elo+pitcher = {accuracy(train_preds_clamped, y_train):.4f}, "
          f"Elo-only = {accuracy(train_elo_only, y_train):.4f}")

    # ============================
    # STEP 3: 2025 held-out walk-forward backtest
    # ============================
    print("\n[STEP 3] 2025 HELD-OUT walk-forward (using fixed 2024 betas)")
    # IMPORTANT: use the 2024-derived league_era so the 2025 backtest is fully
    # walk-forward (no peeking at 2025 aggregates).
    test = walk_forward_collect_with_league_era(con, 2025, prior_season=2024,
                                                  league_era=league_era_2024)
    test_valid = [t for t in test if t["home_starter_id"] is not None
                                      and t["away_starter_id"] is not None]
    print(f"  Collected {len(test)} 2025 rows; {len(test_valid)} with both starters identified")

    # Predict with fixed coefficients from 2024
    preds_with_pitcher = []
    preds_elo_only = []
    actuals = []
    for t in test_valid:
        p_combined = sigmoid(beta_elo * t["elo_log_odds"] + beta_skill * t["skill_delta"])
        p_combined_clamped = max(0.05, min(0.95, p_combined))
        preds_with_pitcher.append(p_combined_clamped)
        preds_elo_only.append(t["p_elo"])
        actuals.append(t["actual_home_win"])

    # ============================
    # STEP 4: Spread-sample audit (leakage check via inline pitcher snapshots)
    # ============================
    print("\n[STEP 4] Spread sample of 20 2025 predictions with EXACT pitcher snapshots used")
    n = len(test_valid)
    idxs = sorted(set([
        0, 1, 2, 3,
        int(n * 0.10), int(n * 0.20), int(n * 0.30), int(n * 0.40),
        int(n * 0.50), int(n * 0.60), int(n * 0.70), int(n * 0.80),
        int(n * 0.85), int(n * 0.90), int(n * 0.95),
        n - 5, n - 4, n - 3, n - 2, n - 1,
    ]))[:20]
    print(f"  {'date':<11s} {'home (Elo, P_skill)':<32s} {'away (Elo, P_skill)':<32s}  "
          f"{'p_elo':>6s} {'p_comb':>6s} {'act':>4s}")
    for i in idxs:
        t = test_valid[i]
        h = f"{t['home_team_name']} ({t['home_rating']:.0f}, {t['home_skill']:+.2f})"
        a = f"{t['away_team_name']} ({t['away_rating']:.0f}, {t['away_skill']:+.2f})"
        p_comb = preds_with_pitcher[i]
        print(f"  {t['game_date']:<11s} {h:<32s} {a:<32s}  "
              f"{t['p_elo']:>6.3f} {p_comb:>6.3f} {t['actual_home_win']:>4d}")

    # ============================
    # STEP 5: Metrics against both gates
    # ============================
    print("\n[STEP 5] 2025 HELD-OUT metrics")
    print(f"  {'Model':<28s} {'Brier':>8s} {'LogLoss':>9s} {'Acc':>8s}")
    print(f"  {'Elo only (prior)':<28s} {brier(preds_elo_only, actuals):>8.4f} {log_loss(preds_elo_only, actuals):>9.4f} {accuracy(preds_elo_only, actuals):>8.4f}")
    print(f"  {'Elo + pitcher (fitted)':<28s} {brier(preds_with_pitcher, actuals):>8.4f} {log_loss(preds_with_pitcher, actuals):>9.4f} {accuracy(preds_with_pitcher, actuals):>8.4f}")

    # 5-bin calibration on the new model
    print("\n[STEP 6] 5-bin calibration — Elo+pitcher held-out 2025")
    print(f"  {'bin':<14s} {'n':>5s} {'mean_pred':>11s} {'observed':>10s} {'gap':>8s}")
    for label, n_bin, mp, obs in calibration_table(preds_with_pitcher, actuals):
        gap = obs - mp if n_bin > 0 else 0.0
        print(f"  {label:<14s} {n_bin:>5d} {mp:>11.4f} {obs:>10.4f} {gap:>+8.4f}")

    # ============================
    # STEP 7: Verdict against both gates
    # ============================
    new_brier = brier(preds_with_pitcher, actuals)
    elo_prior = 0.2429   # from Phase 2 schedule-only backtest
    dossier_gate = 0.22
    print("\n" + "=" * 78)
    print(" VERDICT (held-out 2025)")
    print(f"  Brier (Elo + pitcher fitted): {new_brier:.4f}")
    print(f"  Elo-only prior (Phase 2):     {elo_prior:.4f}")
    print(f"  Dossier gate target:          {dossier_gate:.4f}")
    print()
    if new_brier < elo_prior:
        improvement = elo_prior - new_brier
        print(f"  vs ELO-ONLY PRIOR: IMPROVEMENT of {improvement:+.4f}  (the pitcher feature added signal)")
    elif new_brier > elo_prior:
        deg = new_brier - elo_prior
        print(f"  vs ELO-ONLY PRIOR: DEGRADATION of {deg:+.4f}  (the pitcher feature hurt — investigate)")
    else:
        print(f"  vs ELO-ONLY PRIOR: NEUTRAL")
    print()
    if new_brier <= dossier_gate:
        print(f"  vs DOSSIER GATE 0.22: PASSED ({new_brier:.4f} <= 0.22)")
    else:
        print(f"  vs DOSSIER GATE 0.22: FAILED ({new_brier:.4f} > 0.22, margin {new_brier - dossier_gate:+.4f})")
    print("=" * 78)

    con.close()
    return 0


def walk_forward_collect_with_league_era(con, season, prior_season, league_era):
    """Same as walk_forward_collect, but with an externally-supplied league
    avg ERA (so the 2025 backtest can use the 2024-derived one — fully
    held-out). Code duplication is small; alternative is to refactor
    walk_forward_collect with an optional league_era parameter, which we'd
    do for v2 cleanup."""
    seed_rows = list(con.execute(
        "SELECT team_id, rating FROM elo_ratings WHERE season = ?", (prior_season,)
    ))
    if len(seed_rows) != 30:
        raise RuntimeError(f"Expected 30 teams in {prior_season} EOS Elo, got {len(seed_rows)}")
    ratings = {r["team_id"]: elo.regress_to_mean(r["rating"]) for r in seed_rows}

    games = list(con.execute(
        """SELECT g.game_pk, g.game_date, g.home_team_id, g.home_team_name,
                  g.away_team_id, g.away_team_name, g.home_score, g.away_score,
                  gsp.home_starter_id, gsp.away_starter_id
           FROM games g
           LEFT JOIN game_starting_pitchers gsp ON gsp.game_pk = g.game_pk
           WHERE g.season = ? AND g.game_type = 'R'
                 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                 AND g.home_score != g.away_score
           ORDER BY g.game_date, g.game_pk""",
        (season,),
    ))

    out = []
    for g in games:
        h_id, a_id = g["home_team_id"], g["away_team_id"]
        if h_id not in ratings or a_id not in ratings:
            continue
        h_rating = ratings[h_id]
        a_rating = ratings[a_id]
        p_elo = elo.expected_win_probability(h_rating, a_rating)

        home_skill = pf.pitcher_skill_as_of(con, g["home_starter_id"], g["game_date"], league_era)
        away_skill = pf.pitcher_skill_as_of(con, g["away_starter_id"], g["game_date"], league_era)
        skill_delta = home_skill - away_skill
        actual = int(g["home_score"] > g["away_score"])

        out.append({
            "game_pk": g["game_pk"], "game_date": g["game_date"],
            "home_team_id": h_id, "home_team_name": g["home_team_name"],
            "away_team_id": a_id, "away_team_name": g["away_team_name"],
            "home_rating": h_rating, "away_rating": a_rating,
            "home_starter_id": g["home_starter_id"],
            "away_starter_id": g["away_starter_id"],
            "home_skill": home_skill, "away_skill": away_skill,
            "skill_delta": skill_delta,
            "p_elo": p_elo,
            "elo_log_odds": logit(p_elo),
            "actual_home_win": actual,
            "league_era_used": league_era,
        })

        h_state = elo.EloState(team_id=h_id, rating=h_rating, games_played=0,
                               last_updated_game_pk=None, season=season)
        a_state = elo.EloState(team_id=a_id, rating=a_rating, games_played=0,
                               last_updated_game_pk=None, season=season)
        new_h, new_a = elo.update_after_game(h_state, a_state, g["home_score"],
                                              g["away_score"], g["game_pk"])
        ratings[h_id] = new_h.rating
        ratings[a_id] = new_a.rating

    return out


if __name__ == "__main__":
    raise SystemExit(main())
