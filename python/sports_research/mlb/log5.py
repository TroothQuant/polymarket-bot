"""Bill James log5 formula for head-to-head win probability.

P(A beats B) = (pA - pA * pB) / (pA + pB - 2 * pA * pB)

Where pA and pB are each team's overall win rate. The canonical "two teams of
known overall strength play each other" probability — no parameters, no
training, mathematically clean.

Used as a stat-based companion to the Elo rating system in Phase 2's baseline.
The two are combined (currently as a simple average; weighting is a Phase 4+
calibration concern, not v1) to produce baseline_probability.
"""
from __future__ import annotations


def log5(p_a: float, p_b: float) -> float:
    """Bill James log5: probability that team A beats team B given each team's
    overall win rate. Returns 0.5 if both teams have identical win rates.

    Both inputs must be in (0, 1). Domain edge cases:
      log5(1.0, 0.5) -> 1.0  (an undefeated team always beats an average team)
      log5(0.5, 0.5) -> 0.5  (two .500 teams are coin flips)
      log5(0.0, 0.5) -> 0.0  (a winless team never wins)

    The formula is symmetric and self-consistent: log5(pA, pB) + log5(pB, pA) = 1.0.
    """
    if p_a in (0.0, 1.0):
        return p_a
    if p_b in (0.0, 1.0):
        return 1.0 - p_b
    numerator = p_a - p_a * p_b
    denominator = p_a + p_b - 2.0 * p_a * p_b
    return numerator / denominator
