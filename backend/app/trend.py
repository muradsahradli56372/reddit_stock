"""Trend score (0-100) and trend class. Pure functions, no I/O.

Goal: measure how unusual this week's attention is *relative to the stock's own baseline*,
not raw volume. NVDA at 80 mentions every week is STABLE; RKLB going 6 -> 40 is EMERGING.

Formula
-------
baseline_m = average weekly mentions over up to 4 previous weeks (0 when never mentioned)
baseline_a = same for unique authors

1. Smoothed log growth (k = 5 pseudo-mentions):
       g = log2((current + k) / (baseline + k))         clamped to [-3, +3]
   The +k smoothing stops tiny numbers exploding: 1 -> 3 mentions is g = 0.42, not "+200%".
   Mapped to 0..1 with 0.5 = flat:   s = (g + 3) / 6

2. Core = 0.6 * s_mentions + 0.4 * s_unique_authors
   Unique-author growth carries 40% so one account spamming can't fake a trend.

3. Breadth: growth only counts fully when it is spread across subreddits.
       d = min(subreddit_count / min(4, subreddits_in_dataset), 1)
       if core > 0.5:  core = 0.5 + (core - 0.5) * (0.5 + 0.5 * d)
   (A spike confined to one subreddit keeps ~half of its upside. Cooling is not affected.)

4. Minimum-volume floor: scores of low-volume stocks are pulled toward 50 (neutral).
       w = min(1, max(current, baseline) / 15)
       score = 50 + (100 * core - 50) * w

Classes
-------
EMERGING  score >= 75 AND mentions >= 8 AND mentions grew >= 100% (or are new)
RISING    score >= 60
COOLING   score <= 40
STABLE    otherwise
"""
from __future__ import annotations

import math

SMOOTHING = 5.0
FULL_VOLUME = 15.0
MIN_EMERGING_MENTIONS = 8
W_MENTIONS, W_AUTHORS = 0.6, 0.4


def _growth_component(current: float, baseline: float) -> float:
    g = math.log2((current + SMOOTHING) / (baseline + SMOOTHING))
    g = max(-3.0, min(3.0, g))
    return (g + 3.0) / 6.0


def trend_score(current_mentions: int, baseline_mentions: float,
                current_authors: int, baseline_authors: float,
                subreddit_count: int, subreddits_in_dataset: int = 4) -> float:
    core = (W_MENTIONS * _growth_component(current_mentions, baseline_mentions)
            + W_AUTHORS * _growth_component(current_authors, baseline_authors))
    target = max(1, min(4, subreddits_in_dataset))
    breadth = min(subreddit_count / target, 1.0)
    if core > 0.5:
        core = 0.5 + (core - 0.5) * (0.5 + 0.5 * breadth)
    weight = min(1.0, max(current_mentions, baseline_mentions) / FULL_VOLUME)
    score = 50.0 + (100.0 * core - 50.0) * weight
    return round(max(0.0, min(100.0, score)), 1)


def classify(score: float, current_mentions: int, baseline_mentions: float) -> str:
    grew_100 = baseline_mentions == 0 or current_mentions >= 2 * baseline_mentions
    if score >= 75 and current_mentions >= MIN_EMERGING_MENTIONS and grew_100:
        return "EMERGING"
    if score >= 60:
        return "RISING"
    if score <= 40:
        return "COOLING"
    return "STABLE"
