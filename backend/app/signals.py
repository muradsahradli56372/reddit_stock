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

With Reddit attention (ApeWisdom counts, v0.3) available for this and earlier weeks:
  * eligible also when Reddit mentions >= 30
  * G = max(text growth, Reddit growth with +10 smoothing)
  * L uses the Reddit volume (complete, unlike the sampled StockTwits text), RELATIVE to the
    week's most-mentioned ticker so it works at any data scale: 1 up to 25% of the leader's
    count, falling linearly to 0 at 75% ("early" = not already among the most discussed)
  * E falls back to Reddit upvotes per mention vs. the week's median when there is no text
  * A (unique authors) only exists for text; without text it is 0 (we can't see the people)
Without Reddit data the score is exactly the single-source formula above.

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
REDDIT_MIN_MENTIONS = 30
REDDIT_LOW_FULL_SHARE = 0.25
REDDIT_LOW_ZERO_SHARE = 0.75


def _growth(cur: float, base: float, k: float = 3) -> float:
    return max(0.0, min(1.0, math.log2((cur + k) / (base + k)) / 3))


def low_volume_factor(mentions: float, full: float = LOW_VOLUME_FULL, zero: float = LOW_VOLUME_ZERO) -> float:
    if mentions <= full:
        return 1.0
    if mentions >= zero:
        return 0.0
    return 1.0 - (mentions - full) / (zero - full)


def engagement_component(avg_engagement: float, market_median: float) -> float:
    if market_median <= 0:
        return 0.5 if avg_engagement > 0 else 0.0
    ratio = max(avg_engagement, 0.0) / market_median
    return max(0.0, min(1.0, math.log2(1 + ratio) / math.log2(3)))


def early_signal_score(mentions: int, baseline_mentions: float, unique_authors: int, baseline_authors: float,
                       subreddit_count: int, avg_engagement: float, market_median_engagement: float,
                       reddit_current: float | None = None, reddit_baseline: float | None = None,
                       reddit_upvotes_per_mention: float | None = None,
                       reddit_median_upvotes_per_mention: float | None = None,
                       reddit_week_max: float | None = None) -> float:
    text_ok = mentions >= MIN_MENTIONS and unique_authors >= MIN_AUTHORS
    reddit_known = reddit_current is not None and reddit_baseline is not None
    reddit_ok = reddit_known and reddit_current >= REDDIT_MIN_MENTIONS
    if not text_ok and not reddit_ok:
        return 0.0
    g = _growth(mentions, baseline_mentions) if text_ok else 0.0
    a = _growth(unique_authors, baseline_authors) if text_ok else 0.0
    if reddit_ok:
        g = max(g, _growth(reddit_current, reddit_baseline, 10))
    d = min(subreddit_count / 3, 1.0)
    if text_ok:
        e = engagement_component(avg_engagement, market_median_engagement)
    else:
        e = engagement_component(reddit_upvotes_per_mention or 0.0, reddit_median_upvotes_per_mention or 0.0)
    if reddit_known and reddit_week_max:
        lv = low_volume_factor(reddit_current / reddit_week_max, REDDIT_LOW_FULL_SHARE, REDDIT_LOW_ZERO_SHARE)
    else:
        lv = low_volume_factor(mentions)
    score = 100 * lv * (0.35 * g + 0.25 * a + 0.20 * d + 0.20 * e)
    return round(max(0.0, min(100.0, score)), 1)


def is_early_signal(score: float, mentions: int, baseline_mentions: float,
                    reddit_current: float | None = None, reddit_baseline: float | None = None) -> bool:
    grew = mentions > baseline_mentions or (
        reddit_current is not None and reddit_baseline is not None and reddit_current > reddit_baseline)
    return score >= THRESHOLD and grew
