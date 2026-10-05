"""Early Signal Score (0-100). Pure functions, no I/O.

The trend score answers "is attention unusual for THIS stock?". The early signal score asks
a narrower question: "is a SMALL stock starting to get broad, engaged attention before it is
mainstream?" It deliberately favours low absolute volume.

Eligibility (else score 0): mentions >= 5 AND unique authors >= 4. (Below that, anything is noise.)

Components, each 0..1:
  G  mention growth       clamp(log2((m + 3) / (baseline_m + 3)) / 3, 0, 1)   8x growth -> 1
  A  author growth        same formula on unique authors                     (spam-resistant)
  D  subreddit diversity  min(subreddits / 3, 1)
  E  engagement           clamp(log2(1 + avg_score / market_median) / log2(3), 0, 1)
                          (2x the week's median engagement -> 1; median -> 0.63)
  L  low-volume factor    1 up to 30 mentions, falling linearly to 0 at 120 mentions
                          ("early" means not already one of the most discussed names)

score = 100 * L * (0.35 G + 0.25 A + 0.20 D + 0.20 E)
Flag: is_early_signal = score >= 50 and mentions grew vs baseline.

Smoothing (+3) keeps 1 -> 3 mentions from looking like a breakout. This is a screening
heuristic for further research, not a prediction.
"""
from __future__ import annotations

import math

MIN_MENTIONS = 5
MIN_AUTHORS = 4
LOW_VOLUME_FULL = 30
LOW_VOLUME_ZERO = 120
THRESHOLD = 50.0


def _growth(cur: float, base: float) -> float:
    return max(0.0, min(1.0, math.log2((cur + 3) / (base + 3)) / 3))


def low_volume_factor(mentions: int) -> float:
    if mentions <= LOW_VOLUME_FULL:
        return 1.0
    if mentions >= LOW_VOLUME_ZERO:
        return 0.0
    return 1.0 - (mentions - LOW_VOLUME_FULL) / (LOW_VOLUME_ZERO - LOW_VOLUME_FULL)


def engagement_component(avg_engagement: float, market_median: float) -> float:
    if market_median <= 0:
        return 0.5 if avg_engagement > 0 else 0.0
    ratio = max(avg_engagement, 0.0) / market_median
    return max(0.0, min(1.0, math.log2(1 + ratio) / math.log2(3)))


def early_signal_score(mentions: int, baseline_mentions: float, unique_authors: int, baseline_authors: float,
                       subreddit_count: int, avg_engagement: float, market_median_engagement: float) -> float:
    if mentions < MIN_MENTIONS or unique_authors < MIN_AUTHORS:
        return 0.0
    g = _growth(mentions, baseline_mentions)
    a = _growth(unique_authors, baseline_authors)
    d = min(subreddit_count / 3, 1.0)
    e = engagement_component(avg_engagement, market_median_engagement)
    score = 100 * low_volume_factor(mentions) * (0.35 * g + 0.25 * a + 0.20 * d + 0.20 * e)
    return round(max(0.0, min(100.0, score)), 1)


def is_early_signal(score: float, mentions: int, baseline_mentions: float) -> bool:
    return score >= THRESHOLD and mentions > baseline_mentions
