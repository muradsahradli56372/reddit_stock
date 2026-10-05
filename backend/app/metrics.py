"""Weekly aggregation and week-over-week math. Pure functions, no I/O, fully deterministic."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import statistics

from . import signals, trend


@dataclass(frozen=True)
class MentionRow:
    ticker: str
    author: str | None  # None = deleted / unknown author (excluded from unique-author counts)
    source_type: str  # "post" | "comment"
    subreddit: str
    sentiment: str | None
    engagement: int = 0  # score (upvotes) of the post/comment


@dataclass
class StockWeek:
    ticker: str
    mentions: int = 0
    post_mentions: int = 0
    comment_mentions: int = 0
    authors: set = field(default_factory=set)
    author_counts: Counter = field(default_factory=Counter)
    sentiment: Counter = field(default_factory=Counter)
    subreddits: Counter = field(default_factory=Counter)
    engagement_sum: int = 0

    @property
    def avg_engagement(self) -> float:
        return self.engagement_sum / self.mentions if self.mentions else 0.0

    @property
    def unique_authors(self) -> int:
        return len(self.authors)

    @property
    def top_author_share(self) -> float:
        """Share of mentions produced by the single most active author (0..1)."""
        if not self.author_counts or not self.mentions:
            return 0.0
        return self.author_counts.most_common(1)[0][1] / self.mentions


def pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def pct_change(current: float, previous: float) -> float | None:
    """Week-over-week % change. None when previous == 0 (a 'new' stock: % change is undefined)."""
    if not previous:
        return None
    return round(100.0 * (current - previous) / previous, 1)


def aggregate_week(rows: list[MentionRow]) -> dict[str, StockWeek]:
    out: dict[str, StockWeek] = {}
    for r in rows:
        s = out.get(r.ticker)
        if s is None:
            s = out[r.ticker] = StockWeek(r.ticker)
        s.mentions += 1
        if r.source_type == "post":
            s.post_mentions += 1
        else:
            s.comment_mentions += 1
        if r.author:
            s.authors.add(r.author)
            s.author_counts[r.author] += 1
        s.sentiment[r.sentiment or "unclear"] += 1
        s.subreddits[r.subreddit] += 1
        s.engagement_sum += max(r.engagement or 0, 0)
    return out


@dataclass
class Baseline:
    """Previous-week values and multi-week averages for one ticker."""
    prev_mentions: int = 0
    prev_unique_authors: int = 0
    prev_comment_mentions: int = 0
    avg_mentions: float = 0.0
    avg_unique_authors: float = 0.0


def build_baselines(history: list[dict[str, StockWeek]]) -> dict[str, Baseline]:
    """history: aggregated weeks, most recent previous week first (up to 4).

    Weeks where a ticker was not mentioned count as 0 in its average.
    """
    if not history:
        return {}
    tickers = set().union(*[h.keys() for h in history])
    n = len(history)
    out = {}
    for tk in tickers:
        prev = history[0].get(tk)
        out[tk] = Baseline(
            prev_mentions=prev.mentions if prev else 0,
            prev_unique_authors=prev.unique_authors if prev else 0,
            prev_comment_mentions=prev.comment_mentions if prev else 0,
            avg_mentions=sum(h[tk].mentions for h in history if tk in h) / n,
            avg_unique_authors=sum(h[tk].unique_authors for h in history if tk in h) / n,
        )
    return out


def build_metric_rows(current: dict[str, StockWeek], baselines: dict[str, Baseline],
                      subreddits_in_dataset: int) -> list[dict]:
    """Produce one metrics dict per ticker mentioned this week, ranked by mentions."""
    rows = []
    eligible = [s.avg_engagement for s in current.values() if s.mentions >= signals.MIN_MENTIONS]
    median_eng = statistics.median(eligible) if eligible else 0.0
    for tk, s in current.items():
        b = baselines.get(tk, Baseline())
        early = signals.early_signal_score(s.mentions, b.avg_mentions, s.unique_authors, b.avg_unique_authors,
                                           len(s.subreddits), s.avg_engagement, median_eng)
        score = trend.trend_score(s.mentions, b.avg_mentions, s.unique_authors, b.avg_unique_authors,
                                  len(s.subreddits), subreddits_in_dataset)
        rows.append({
            "ticker": tk,
            "mentions": s.mentions,
            "unique_authors": s.unique_authors,
            "post_mentions": s.post_mentions,
            "comment_mentions": s.comment_mentions,
            "bullish": s.sentiment["bullish"],
            "neutral": s.sentiment["neutral"],
            "bearish": s.sentiment["bearish"],
            "unclear": s.sentiment["unclear"],
            "bullish_pct": pct(s.sentiment["bullish"], s.mentions),
            "neutral_pct": pct(s.sentiment["neutral"], s.mentions),
            "bearish_pct": pct(s.sentiment["bearish"], s.mentions),
            "subreddit_count": len(s.subreddits),
            "subreddit_distribution": dict(s.subreddits.most_common()),
            "top_author_share": round(s.top_author_share, 3),
            "prev_mentions": b.prev_mentions,
            "prev_unique_authors": b.prev_unique_authors,
            "prev_comment_mentions": b.prev_comment_mentions,
            "mention_change_pct": pct_change(s.mentions, b.prev_mentions),
            "author_change_pct": pct_change(s.unique_authors, b.prev_unique_authors),
            "comment_change_pct": pct_change(s.comment_mentions, b.prev_comment_mentions),
            "trend_score": score,
            "trend_class": trend.classify(score, s.mentions, b.avg_mentions),
            "avg_engagement": round(s.avg_engagement, 1),
            "early_signal_score": early,
            "is_early_signal": signals.is_early_signal(early, s.mentions, b.avg_mentions),
        })
    rows.sort(key=lambda r: (-r["mentions"], -r["unique_authors"], r["ticker"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def sentiment_totals(rows: list[MentionRow]) -> dict:
    c = Counter(r.sentiment or "unclear" for r in rows)
    total = len(rows)
    return {label: {"count": c[label], "pct": pct(c[label], total)}
            for label in ("bullish", "neutral", "bearish", "unclear")} | {"total": total}


def subreddit_mentions(rows: list[MentionRow]) -> dict[str, int]:
    d: dict[str, int] = defaultdict(int)
    for r in rows:
        d[r.subreddit] += 1
    return dict(d)
