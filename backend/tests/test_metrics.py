import pytest

from app import trend
from app.metrics import (MentionRow, aggregate_week, build_baselines, build_metric_rows, pct_change,
                         sentiment_totals)


def rows(ticker, n, authors, sub="stocks", source="comment", sentiment="bullish"):
    return [MentionRow(ticker, authors[i % len(authors)], source, sub, sentiment) for i in range(n)]


def test_unique_authors_not_mentions():
    # One user with 500 comments is 1 author, not 500.
    data = rows("NVDA", 500, ["heavy_user"]) + rows("NVDA", 10, [f"u{i}" for i in range(10)])
    s = aggregate_week(data)["NVDA"]
    assert s.mentions == 510
    assert s.unique_authors == 11
    assert s.top_author_share == pytest.approx(500 / 510)


def test_deleted_authors_excluded_from_unique_count():
    s = aggregate_week(rows("TSLA", 3, [None]) + rows("TSLA", 2, ["a", "b"]))["TSLA"]
    assert s.mentions == 5 and s.unique_authors == 2


def test_post_comment_and_subreddit_split():
    data = rows("PLTR", 3, ["a"], sub="stocks", source="post") + rows("PLTR", 5, ["b"], sub="wallstreetbets")
    s = aggregate_week(data)["PLTR"]
    assert (s.post_mentions, s.comment_mentions) == (3, 5)
    assert dict(s.subreddits) == {"stocks": 3, "wallstreetbets": 5}


@pytest.mark.parametrize("cur,prev,expected", [
    (20, 10, 100.0), (5, 10, -50.0), (10, 10, 0.0), (7, 3, 133.3), (5, 0, None), (0, 0, None),
])
def test_pct_change(cur, prev, expected):
    assert pct_change(cur, prev) == expected


def test_sentiment_percentages():
    data = (rows("AAPL", 6, ["a"], sentiment="bullish") + rows("AAPL", 3, ["b"], sentiment="bearish")
            + rows("AAPL", 1, ["c"], sentiment="neutral"))
    [r] = build_metric_rows(aggregate_week(data), {}, 4)
    assert (r["bullish"], r["bearish"], r["neutral"]) == (6, 3, 1)
    assert (r["bullish_pct"], r["bearish_pct"], r["neutral_pct"]) == (60.0, 30.0, 10.0)
    totals = sentiment_totals(data)
    assert totals["bullish"]["pct"] == 60.0 and totals["total"] == 10


def test_week_over_week_rows():
    prev = aggregate_week(rows("RKLB", 5, ["a", "b"]) + rows("NVDA", 40, [f"u{i}" for i in range(30)]))
    cur = aggregate_week(rows("RKLB", 20, [f"x{i}" for i in range(15)])
                         + rows("NVDA", 40, [f"u{i}" for i in range(30)]) + rows("ASTS", 9, ["z"]))
    out = {r["ticker"]: r for r in build_metric_rows(cur, build_baselines([prev]), 4)}
    assert out["RKLB"]["prev_mentions"] == 5
    assert out["RKLB"]["mention_change_pct"] == 300.0
    assert out["RKLB"]["author_change_pct"] == 650.0  # 2 -> 15 authors
    assert out["NVDA"]["mention_change_pct"] == 0.0
    assert out["ASTS"]["mention_change_pct"] is None  # new: no previous mentions
    assert [r["ticker"] for r in sorted(out.values(), key=lambda r: r["rank"])] == ["NVDA", "RKLB", "ASTS"]


def test_baseline_averages_over_weeks_with_zero_fill():
    w1 = aggregate_week(rows("SOFI", 10, ["a"]))
    w2 = aggregate_week(rows("AMD", 4, ["b"]))  # SOFI not mentioned this week -> counts as 0
    b = build_baselines([w1, w2])
    assert b["SOFI"].prev_mentions == 10 and b["SOFI"].avg_mentions == 5.0


# ---------------------------------------------------------------- trend score
def test_flat_stock_scores_neutral():
    assert trend.trend_score(80, 80, 60, 60, 5) == 50.0
    assert trend.classify(50.0, 80, 80) == "STABLE"


def test_spike_is_emerging():
    s = trend.trend_score(40, 6, 30, 5, 5)
    assert s >= 75 and trend.classify(s, 40, 6) == "EMERGING"


def test_tiny_numbers_do_not_produce_absurd_scores():
    # 1 -> 4 mentions is +300% but should not be a strong signal.
    s = trend.trend_score(4, 1, 4, 1, 2)
    assert 50 < s < 60
    assert trend.classify(s, 4, 1) == "STABLE"


def test_volume_alone_is_not_trend():
    # A huge but flat stock scores lower than a small stock growing fast from its own baseline.
    assert trend.trend_score(500, 480, 300, 290, 5) < trend.trend_score(30, 8, 25, 7, 5)


def test_author_spam_scores_lower_than_broad_growth():
    spam = trend.trend_score(40, 10, 3, 3, 4)  # mentions up, same few people
    broad = trend.trend_score(40, 10, 30, 8, 4)
    assert broad > spam


def test_narrow_subreddit_growth_scores_lower():
    assert trend.trend_score(40, 6, 30, 5, 1) < trend.trend_score(40, 6, 30, 5, 5)


def test_cooling():
    s = trend.trend_score(30, 70, 25, 55, 5)
    assert s <= 40 and trend.classify(s, 30, 70) == "COOLING"


def test_score_bounds():
    for args in [(0, 1000, 0, 1000, 1), (10000, 0, 10000, 0, 10), (0, 0, 0, 0, 0)]:
        assert 0 <= trend.trend_score(*args) <= 100


def test_rising_but_not_doubled_is_not_emerging():
    s = trend.trend_score(55, 30, 45, 22, 5)
    assert trend.classify(s, 55, 30) in {"RISING", "STABLE"}
    assert trend.classify(90.0, 55, 30) == "RISING"  # high score but < +100% growth
