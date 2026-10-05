"""StockTwits + ApeWisdom: parsers, collectors (mocked HTTP), aggregation, combined scoring, pipeline."""
from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app import attention, metrics, signals, trend
from app.attention import ApeWisdomProvider, RedditWeek, SnapshotRow
from app.collectors.stocktwits import StockTwitsCollector, parse_message
from app.config import settings
from app.db import session_scope
from app.http_client import JsonClient

WEEK = date(2026, 6, 1)  # far from the demo weeks so these tests don't disturb them


def msg(mid, created, body="$NVDA to the moon", user="alice", label=None, likes=2):
    return {"id": mid, "body": body, "created_at": created, "user": {"username": user},
            "entities": {"sentiment": {"basic": label} if label else None}, "likes": {"total": likes},
            "conversation": {"replies": 1}}


def client_for(handler, sleeps=None):
    return JsonClient(transport=httpx.MockTransport(handler), sleep=(sleeps.append if sleeps is not None else
                                                                     (lambda s: None)), max_retries=3)


# ---------------------------------------------------------------- StockTwits parsing
def test_parse_message_full():
    p = parse_message(msg(42, "2026-06-02T10:00:00Z", label="Bullish", likes=7))
    assert p.reddit_id == "st_42" and p.subreddit == "stocktwits" and p.author == "st:alice"
    assert p.author_sentiment == "bullish" and p.score == 7 and p.num_comments == 1
    assert p.created_utc == datetime(2026, 6, 2, 10, 0)
    assert p.permalink == "https://stocktwits.com/alice/message/42"


@pytest.mark.parametrize("bad", [{"id": 1}, {"id": 1, "body": "", "created_at": "2026-06-02T10:00:00Z"},
                                 {"body": "x", "created_at": "2026-06-02T10:00:00Z"},
                                 {"id": 1, "body": "x", "created_at": "not a date"}])
def test_parse_message_rejects_unusable(bad):
    assert parse_message(bad) is None


def test_parse_message_tolerates_missing_optional_fields():
    p = parse_message({"id": 5, "body": "$TSLA", "created_at": "2026-06-02T10:00:00+00:00"})
    assert p.author is None and p.author_sentiment is None and p.score == 0


# ---------------------------------------------------------------- StockTwits collector
def test_stocktwits_pages_window_and_dedup(monkeypatch):
    monkeypatch.setattr(settings, "stocktwits_request_delay", 0)
    pages = {
        ("NVDA", None): [msg(300, "2026-06-08T01:00:00Z"),  # after the week -> skipped
                         msg(299, "2026-06-07T23:00:00Z"), msg(298, "2026-06-05T12:00:00Z", "$NVDA $AMD")],
        ("NVDA", "297"): [msg(297, "2026-06-01T00:30:00Z"), msg(200, "2026-05-31T23:00:00Z")],  # crosses start
        ("AMD", None): [msg(298, "2026-06-05T12:00:00Z", "$NVDA $AMD")],  # same message in AMD stream
    }
    seen = []

    def handler(req):
        sym = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        seen.append((sym, req.url.params.get("max")))
        return httpx.Response(200, json={"messages": pages.get((sym, req.url.params.get("max")), [])})
    c = StockTwitsCollector(client=client_for(handler), sleep=lambda s: None)
    c.symbols = ["NVDA", "AMD"]
    out = c.collect(WEEK)
    assert sorted(p.reddit_id for p in out.posts) == ["st_297", "st_298", "st_299"]
    assert out.platform == "stocktwits"
    assert seen == [("NVDA", None), ("AMD", None), ("NVDA", "297"), ("AMD", "297")]  # breadth-first
    assert out.stats["symbols"]["NVDA"]["window_reached"] is True


def test_stocktwits_stops_at_known_ids_and_respects_budget(monkeypatch):
    monkeypatch.setattr(settings, "stocktwits_request_delay", 0)
    monkeypatch.setattr(settings, "stocktwits_max_requests", 2)
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"messages": [msg(10, "2026-06-03T00:00:00Z"),
                                                      msg(9, "2026-06-02T00:00:00Z")]})
    c = StockTwitsCollector(client=client_for(handler), sleep=lambda s: None)
    c.symbols = ["NVDA", "TSLA", "AAPL"]
    c.known_ids = {"st_9"}
    out = c.collect(WEEK)
    assert [p.reddit_id for p in out.posts] == ["st_10"]  # stopped at the known message
    assert len(calls) == 2  # NVDA + TSLA, then budget exhausted -> AAPL skipped
    assert out.stats["symbols"]["AAPL"]["pages"] == 0  # never read, and reported as such


def test_stocktwits_blocked_symbol_does_not_stop_run(monkeypatch):
    monkeypatch.setattr(settings, "stocktwits_request_delay", 0)

    def handler(req):
        if "BAD" in req.url.path:
            return httpx.Response(403, text="blocked")
        return httpx.Response(200, json={"messages": [msg(1, "2026-06-02T00:00:00Z")]})
    c = StockTwitsCollector(client=client_for(handler), sleep=lambda s: None)
    c.symbols = ["BAD", "NVDA"]
    out = c.collect(WEEK)
    assert out.stats["symbols"]["BAD"]["error"] == "HTTP 403"
    assert [p.reddit_id for p in out.posts] == ["st_1"]


def test_http_client_honours_retry_after():
    state, sleeps = {"n": 0}, []

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})
    assert client_for(handler, sleeps).get_json("https://x/y") == {"ok": True}
    assert sleeps == [7.0]


def test_trending():
    c = StockTwitsCollector(client=client_for(
        lambda req: httpx.Response(200, json={"symbols": [{"symbol": "oklo"}, {"symbol": "NVDA"}, {},
                                                          {"symbol": "WIF.X"}]})))
    assert c.trending() == ["OKLO", "NVDA"]


# ---------------------------------------------------------------- ApeWisdom
def test_apewisdom_pages_and_bad_filter(monkeypatch):
    monkeypatch.setattr(settings, "apewisdom_filters", ["wallstreetbets", "nosuchfilter"])
    monkeypatch.setattr(settings, "apewisdom_max_pages", 2)
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if "nosuchfilter" in req.url.path:
            return httpx.Response(404)
        page = int(req.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"pages": "5", "currentPage": page, "results": [
            {"rank": page, "ticker": f"t{page}", "name": "Co", "mentions": "1,234", "upvotes": 10},
            {"rank": 9, "ticker": "", "mentions": 3}]})
    rows = ApeWisdomProvider(client_for(handler)).snapshot(WEEK)
    assert [(r.community, r.ticker, r.mentions) for r in rows] == [("wallstreetbets", "T1", 1234),
                                                                   ("wallstreetbets", "T2", 1234)]
    assert len(seen) == 3  # 2 pages (capped, though 5 exist) + 1 failing filter


def test_weekly_estimate_from_daily_snapshots():
    d = [date(2026, 6, 1 + i) for i in range(7)]
    rows = [("wsb", "NVDA", d[0], 100, 500), ("wsb", "NVDA", d[1], 120, 600),
            ("wsb", "RKLB", d[1], 10, 40),  # RKLB absent on d0 -> counts as 0 that day
            ("stocks", "NVDA", d[0], 30, 90)]
    out, days = attention.aggregate_week(rows)
    assert days == 2
    assert out["NVDA"].distribution == {"wsb": 770.0, "stocks": 210.0}  # (220/2)*7, (30/1)*7
    assert out["NVDA"].mentions == 980.0
    assert out["RKLB"].mentions == 35.0 and out["RKLB"].days_covered == 2


def test_aggregate_empty():
    assert attention.aggregate_week([]) == ({}, 0)


# ---------------------------------------------------------------- combined scoring
def test_single_source_scores_unchanged_when_reddit_absent():
    assert trend.trend_score(40, 6, 30, 5, 5) == trend.trend_score(40, 6, 30, 5, 5, 4, None, None)
    assert signals.early_signal_score(25, 4, 20, 4, 4, 200, 100) == \
        signals.early_signal_score(25, 4, 20, 4, 4, 200, 100, None, None)


def test_reddit_growth_confirms_trend():
    flat_text = trend.trend_score(30, 30, 20, 20, 2, 4)
    with_reddit_surge = trend.trend_score(30, 30, 20, 20, 4, 6, reddit_current=800, reddit_baseline=150)
    with_reddit_flat = trend.trend_score(30, 30, 20, 20, 4, 6, reddit_current=150, reddit_baseline=150)
    assert with_reddit_surge > flat_text + 5
    assert with_reddit_flat == pytest.approx(flat_text, abs=0.5)


def test_reddit_only_ticker_can_emerge():
    s = trend.trend_score(0, 0, 0, 0, 3, 5, reddit_current=330, reddit_baseline=60)
    assert s >= 75 and trend.classify(s, 0, 0, 330, 60) == "EMERGING"
    assert trend.classify(s, 0, 0, 20, 5) != "EMERGING"  # too little volume


def test_unknown_reddit_baseline_is_ignored():
    assert trend.trend_score(40, 6, 30, 5, 5, 4, reddit_current=500, reddit_baseline=None) == \
        trend.trend_score(40, 6, 30, 5, 5, 4)


def test_early_signal_low_volume_is_relative_to_week_leader():
    small = signals.early_signal_score(0, 0, 0, 0, 3, 0, 0, 300, 50, 5, 5, reddit_week_max=2000)
    leader = signals.early_signal_score(0, 0, 0, 0, 3, 0, 0, 1800, 300, 5, 5, reddit_week_max=2000)
    assert small > 50 and leader == 0.0


def test_metric_rows_with_reddit():
    cur = metrics.aggregate_week([metrics.MentionRow("NVDA", "a", "post", "stocktwits", "bullish", 3)])
    reddit = {"NVDA": RedditWeek("NVDA", 900, 3000, {"wsb": 900}, 7),
              "OKLO": RedditWeek("OKLO", 330, 1000, {"wsb": 200, "stocks": 130}, 7),
              "TINY": RedditWeek("TINY", 5, 10, {"wsb": 5}, 7)}
    rows = {r["ticker"]: r for r in metrics.build_metric_rows(cur, {}, 1, reddit, None, {"NVDA": 880, "OKLO": 60}, 20)}
    assert set(rows) == {"NVDA", "OKLO"}  # TINY below the Reddit floor, OKLO included without text
    assert rows["NVDA"]["rank"] == 1 and rows["OKLO"]["rank"] == 2  # ranked by Reddit attention
    assert rows["OKLO"]["mentions"] == 0 and rows["OKLO"]["reddit_mentions"] == 330
    assert rows["OKLO"]["reddit_change_pct"] is None  # previous week not covered -> no WoW
    assert rows["OKLO"]["subreddit_count"] == 0 and rows["OKLO"]["trend_class"] == "EMERGING"


# ---------------------------------------------------------------- pipeline with StockTwits-like data
class FakeStockTwits:
    name = "stocktwits"
    incremental = True

    def __init__(self, posts):
        self.posts, self.symbols, self.known_ids = posts, [], set()

    def trending(self):
        return ["OKLO"]

    def collect(self, week):
        from app.collectors.base import CollectedWeek
        return CollectedWeek(week, posts=[p for p in self.posts if p.reddit_id not in self.known_ids],
                             platform="stocktwits")


def _posts():
    return [parse_message(m) for m in [
        msg(1001, "2026-06-02T10:00:00Z", "$NVDA looks overvalued here", "u1", label="Bullish"),
        msg(1002, "2026-06-02T11:00:00Z", "$NVDA and $AMD both overvalued", "u2", label="Bullish"),
        msg(1003, "2026-06-03T11:00:00Z", "$TSLA delivery numbers were weak", "u3"),
    ]]


def test_stocktwits_pipeline_author_labels_and_platform(analysed_db):
    from app.models import Post, StockMention, Subreddit
    from app.pipeline import prepare_collector, process_week
    col = FakeStockTwits(_posts())
    col.incremental = False  # WEEK is long finished; force collection for this test
    prepare_collector(col, WEEK)
    assert col.symbols[0:1] and "OKLO" in col.symbols  # universe includes trending
    process_week(col, WEEK)
    with session_scope() as s:
        def sent(rid, tk):
            return s.execute(select(StockMention.sentiment, StockMention.sentiment_method)
                             .join(Post, StockMention.post_id == Post.id)
                             .where(Post.reddit_id == rid, StockMention.ticker == tk)).one()
        # single-ticker message: author's own "Bullish" tag wins over our "overvalued" reading
        assert sent("st_1001", "NVDA") == ("bullish", "author_label")
        # multi-ticker message: tag ambiguous -> our classifier decides
        assert sent("st_1002", "NVDA")[1] == "fallback_keywords"
        assert sent("st_1003", "TSLA") == ("bearish", "fallback_keywords")
        assert s.scalar(select(Subreddit.platform).where(Subreddit.name == "stocktwits")) == "stocktwits"
    # Re-run: already stored messages are skipped by the incremental collector
    prepare_collector(col, WEEK)
    assert {"st_1001", "st_1002", "st_1003"} <= col.known_ids


def test_collect_now_snapshots_and_text(analysed_db, monkeypatch):
    from app import pipeline
    from app.models import AttentionSnapshot

    class FakeApe:
        name, live_only = "apewisdom", True

        def snapshot(self, day):
            return [SnapshotRow("wallstreetbets", "ZZNEW", 42, 100, 1, "New Co")]
    monkeypatch.setattr(pipeline.attention, "get_attention_provider", lambda: FakeApe())
    monkeypatch.setattr(pipeline, "get_collector", lambda: FakeStockTwits([]))
    out = pipeline.collect_now()
    assert out["attention"]["rows"] == 1 and out["text"]["source"] == "stocktwits"
    with session_scope() as s:
        from app.models import Company
        assert s.scalar(select(Company.name).where(Company.ticker == "ZZNEW")) == "New Co"  # auto-added
        assert s.scalar(select(func.count()).select_from(AttentionSnapshot)
                        .where(AttentionSnapshot.ticker == "ZZNEW")) == 1
    pipeline.collect_now()  # same day again -> replaced, not duplicated
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(AttentionSnapshot)
                        .where(AttentionSnapshot.ticker == "ZZNEW")) == 1


# ---------------------------------------------------------------- wording / summary with two sources
def test_price_comparison_prefers_reddit_counts():
    from app.pipeline import attention_change
    row = {"reddit_prev_mentions": 56, "reddit_change_pct": 488.0, "mention_change_pct": None}
    assert attention_change(row, "StockTwits") == (488.0, "Reddit")
    row = {"reddit_prev_mentions": None, "reddit_change_pct": None, "mention_change_pct": 25.0}
    assert attention_change(row, "StockTwits") == (25.0, "StockTwits")
    from app.market_data import describe_attention_vs_price
    assert describe_attention_vs_price(25.0, 3.0, "StockTwits").startswith("StockTwits attention increased")


def test_summary_never_claims_sentiment_for_reddit_only_rows():
    from app.summary import template_summary
    row = {"ticker": "OKLO", "mentions": 0, "unique_authors": 0, "prev_mentions": 0, "prev_unique_authors": 0,
           "mention_change_pct": None, "author_change_pct": None, "bullish_pct": 0.0, "bearish_pct": 0.0,
           "subreddit_count": 0, "subreddit_distribution": {}, "top_author_share": 0.0, "trend_score": 86.0, "trend_class": "EMERGING",
           "reddit_mentions": 329.0, "reddit_prev_mentions": 56.0, "reddit_change_pct": 487.5,
           "reddit_distribution": {"stocks": 109, "wallstreetbets": 73}, "is_early_signal": False}
    overview = {"total_mentions": 0, "stocks_detected": 1, "posts": 0, "comments": 0, "unique_authors": 0,
                "sentiment": {}}
    out = template_summary(overview, [row])
    text = " ".join(out["data"] + out["interpretation"])
    assert "Reddit mentions (est.) 56 -> 329 (+488% WoW)" in text and "not in the text sample" in text
    assert "sentiment and the reasons behind it are unknown" in text
    assert "0% bullish" not in text


def test_live_mode_end_to_end_with_fake_http(analysed_db, monkeypatch):
    """TEXT_SOURCE=stocktwits + ATTENTION_SOURCE=apewisdom through the real collectors/providers,
    with HTTP answered by a fake server."""
    from app import http_client, pipeline
    from app.collectors.base import week_start_of
    from app.models import AttentionSnapshot, Post, StockMention

    monkeypatch.setattr(settings, "text_source", "stocktwits")
    monkeypatch.setattr(settings, "attention_source", "apewisdom")
    monkeypatch.setattr(settings, "apewisdom_filters", ["wallstreetbets"])
    monkeypatch.setattr(settings, "stocktwits_watchlist", ["NVDA"])
    monkeypatch.setattr(settings, "stocktwits_universe_size", 3)
    monkeypatch.setattr(settings, "stocktwits_request_delay", 0)
    now_week = week_start_of(datetime.utcnow())
    requested = []

    def handler(req):
        requested.append(req.url.path)
        if "apewisdom" in req.url.host:
            return httpx.Response(200, json={"pages": 1, "results": [
                {"rank": 1, "ticker": "LIVEX", "name": "Live Example", "mentions": "50", "upvotes": "300"}]})
        if req.url.path.endswith("trending/symbols.json"):
            return httpx.Response(200, json={"symbols": [{"symbol": "AMD"}]})
        sym = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if req.url.params.get("max"):
            return httpx.Response(200, json={"messages": []})
        t = f"{now_week}T00:30:00Z"
        return httpx.Response(200, json={"messages": [
            msg(int(9000 + len(sym)), t, f"${sym} breakout, loading up", f"{sym.lower()}_fan", label="Bullish")]})

    real_init = http_client.JsonClient.__init__

    def fake_init(self, transport=None, sleep=None, max_retries=None, timeout=20.0):
        real_init(self, transport=httpx.MockTransport(handler), sleep=lambda s: None, max_retries=2)
    monkeypatch.setattr(http_client.JsonClient, "__init__", fake_init)

    out = pipeline.collect_now()
    assert out["attention"]["provider"] == "apewisdom" and out["attention"]["rows"] == 1
    assert out["text"]["source"] == "stocktwits" and out["text"]["new_items"] >= 2
    streams = {p.rsplit("/", 1)[-1] for p in requested if "/streams/symbol/" in p}
    assert {"LIVEX.json", "AMD.json", "NVDA.json"} <= streams  # Reddit top + trending + watchlist
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(AttentionSnapshot)
                        .where(AttentionSnapshot.source == "apewisdom")) >= 1
        n_posts = s.scalar(select(func.count()).select_from(Post).where(Post.reddit_id.like("st_%"),
                                                                        Post.week_start == now_week))
    assert n_posts >= 2
    # Analysing the current week: mentions come from StockTwits with the author's own label
    pipeline.process_week(pipeline.get_collector(), now_week)
    with session_scope() as s:
        rows = s.execute(select(StockMention.ticker, StockMention.sentiment_method)
                         .join(Post, StockMention.post_id == Post.id)
                         .where(Post.reddit_id.like("st_%"), StockMention.week_start == now_week)).all()
    assert ("NVDA", "author_label") in rows


# ---------------------------------------------------------------- live-mode first run behaviour
def test_should_collect_only_open_or_just_ended_weeks():
    from app.pipeline import should_collect
    inc = FakeStockTwits([])
    week = date(2026, 9, 28)
    assert should_collect(inc, week, datetime(2026, 10, 1, 12))          # open week
    assert should_collect(inc, week, datetime(2026, 10, 5, 9))           # first day after it ended
    assert not should_collect(inc, week, datetime(2026, 10, 7, 9))       # long finished -> stored data only
    batch = type("Batch", (), {"incremental": False})()
    assert should_collect(batch, week, datetime(2027, 1, 1))  # non-incremental collectors always collect


def test_week_progress():
    from app.pipeline import week_progress
    week = date(2026, 9, 28)
    assert week_progress(week, datetime(2026, 10, 10)) == 1.0
    assert week_progress(week, datetime(2026, 9, 30, 12)) == pytest.approx(2.5 / 7)


def test_partial_week_compares_pace_not_totals(analysed_db, monkeypatch):
    """On Wednesday a stock with the same daily pace as last week must not look like it collapsed."""
    from app import pipeline
    from app.collectors.base import CollectedWeek

    prev_week, cur_week = date(2026, 5, 18), date(2026, 5, 25)
    posts_prev = [parse_message(msg(70000 + i, f"2026-05-{18 + i % 7:02d}T12:00:00Z", "$PLTR contracts", f"p{i}"))
                  for i in range(28)]  # 4/day for 7 days
    posts_cur = [parse_message(msg(71000 + i, f"2026-05-{25 + i % 2:02d}T12:00:00Z", "$PLTR contracts", f"c{i}"))
                 for i in range(8)]   # 4/day for the 2 days so far

    class Fixed:
        name, incremental = "stocktwits", False

        def __init__(self, posts):
            self.posts = posts

        def collect(self, w):
            return CollectedWeek(w, posts=self.posts, platform="stocktwits")
    monkeypatch.setattr(pipeline, "week_progress",
                        lambda w, now=None: 2 / 7 if w == cur_week else 1.0)
    from app.models import CollectionRun
    with session_scope() as s:  # both weeks were collected while open -> valid baseline
        for w in (prev_week, cur_week):
            s.add(CollectionRun(source="stocktwits", week_start=w, first_run_at=datetime(w.year, w.month, w.day, 1),
                                last_run_at=datetime(w.year, w.month, w.day, 23), runs=10))
    pipeline.process_week(Fixed(posts_prev), prev_week)
    pipeline.process_week(Fixed(posts_cur), cur_week)
    from app.models import WeeklyReport, WeeklyStockMetric
    with session_scope() as s:
        m = s.scalar(select(WeeklyStockMetric).where(WeeklyStockMetric.week_start == cur_week,
                                                     WeeklyStockMetric.ticker == "PLTR"))
        ov = s.scalar(select(WeeklyReport.overview).where(WeeklyReport.week_start == cur_week))
    assert m.mentions == 8 and m.prev_mentions == 8  # 28 * 2/7 = pace-adjusted
    assert m.mention_change_pct == 0.0 and m.trend_class == "STABLE"
    assert ov["in_progress"] is True and ov["days_elapsed"] == 2.0


def test_purge_demo_data(analysed_db):
    """Switching to live removes every demo row (and nothing real)."""
    from app import pipeline
    from app.models import AttentionSnapshot, Post, WeeklyReport
    with session_scope() as s:
        real_before = s.scalar(select(func.count()).select_from(Post).where(Post.is_demo.is_(False)))
        assert s.scalar(select(func.count()).select_from(Post).where(Post.is_demo.is_(True))) > 0
    out = pipeline.purge_demo_data()
    assert out["posts"] > 0 and out["reports"] > 0
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Post).where(Post.is_demo.is_(True))) == 0
        assert s.scalar(select(func.count()).select_from(WeeklyReport).where(WeeklyReport.is_demo.is_(True))) == 0
        assert s.scalar(select(func.count()).select_from(AttentionSnapshot)
                        .where(AttentionSnapshot.source == "demo")) == 0
        assert s.scalar(select(func.count()).select_from(Post).where(Post.is_demo.is_(False))) == real_before
    # restore the demo dataset for any later test
    from app.collectors.demo import DemoCollector
    from app.pipeline import run_analysis
    from .conftest import ANCHOR
    run_analysis(ANCHOR, collector=DemoCollector(anchor_week=ANCHOR))


def test_apewisdom_excludes_futures(monkeypatch):
    monkeypatch.setattr(settings, "apewisdom_filters", ["Daytrading"])

    def handler(req):
        return httpx.Response(200, json={"pages": 1, "results": [
            {"ticker": "ES", "mentions": 6}, {"ticker": "MSFT", "mentions": 1}]})
    rows = ApeWisdomProvider(client_for(handler)).snapshot(WEEK)
    assert [r.ticker for r in rows] == ["MSFT"]


def test_stocktwits_budget_is_shared_across_symbols(monkeypatch):
    """A tight budget must reach every symbol once before going deeper into any of them."""
    monkeypatch.setattr(settings, "stocktwits_request_delay", 0)
    monkeypatch.setattr(settings, "stocktwits_max_requests", 5)
    calls = []

    def handler(req):
        sym = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        calls.append(sym)
        base = 10_000 * (len(calls) + 1)
        return httpx.Response(200, json={"messages": [msg(base + i, "2026-06-03T00:00:00Z", f"${sym}")
                                                      for i in range(3)]})
    c = StockTwitsCollector(client=client_for(handler), sleep=lambda s: None)
    c.symbols = ["BUSY", "AAPL", "TSLA", "PLTR"]
    c.collect(WEEK)
    assert calls[:4] == ["BUSY", "AAPL", "TSLA", "PLTR"] and len(calls) == 5


def test_sqlite_uses_wal(tmp_path):
    from sqlalchemy import text
    from app.db import make_engine
    eng = make_engine(f"sqlite:///{tmp_path}/w.db")
    with eng.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"


# ---------------------------------------------------------------- baselines & first-week honesty
def test_no_baseline_means_unrated_not_emerging():
    cur = metrics.aggregate_week([metrics.MentionRow("SPY", f"u{i}", "post", "stocktwits", "neutral", 1)
                                  for i in range(300)])
    [r] = metrics.build_metric_rows(cur, {}, 1, text_baseline_known=False)
    assert r["trend_class"] == "UNRATED" and r["has_baseline"] is False and r["text_baseline"] is False
    assert r["mention_change_pct"] is None and r["early_signal_score"] == 0.0 and not r["is_early_signal"]
    [r2] = metrics.build_metric_rows(cur, {}, 1)  # with a (zero) baseline: genuinely new -> rated
    assert r2["trend_class"] == "EMERGING"


def test_reddit_baseline_rates_even_without_text_baseline():
    cur = metrics.aggregate_week([metrics.MentionRow("RKLB", f"u{i}", "post", "stocktwits", "bullish", 1)
                                  for i in range(40)])
    reddit = {"RKLB": RedditWeek("RKLB", 400, 900, {"wsb": 400}, 7)}
    [r] = metrics.build_metric_rows(cur, {}, 1, reddit, {"RKLB": 60}, {"RKLB": 60}, 20,
                                    text_baseline_known=False)
    assert r["has_baseline"] and not r["text_baseline"] and r["mention_change_pct"] is None
    assert r["trend_class"] == "EMERGING" and r["reddit_change_pct"] > 500


def test_week_collected_late_is_not_a_baseline(analysed_db):
    """Week A collected only after it ended -> week B (collected while open) is UNRATED, not 'all NEW'."""
    from app import pipeline
    from app.collectors.base import CollectedWeek
    from app.models import CollectionRun, WeeklyReport, WeeklyStockMetric
    wa, wb = date(2026, 4, 6), date(2026, 4, 13)

    class Fixed:
        name, incremental = "stocktwits", False

        def __init__(self, posts):
            self.posts = posts

        def collect(self, w):
            return CollectedWeek(w, posts=self.posts, platform="stocktwits")

    def posts(base, day0):
        return [parse_message(msg(base + i, f"2026-04-{day0 + i % 7:02d}T12:00:00Z", "$SPY", f"u{base}{i}"))
                for i in range(20)]
    with session_scope() as s:
        s.add(CollectionRun(source="stocktwits", week_start=wa, first_run_at=datetime(2026, 4, 13, 9),
                            last_run_at=datetime(2026, 4, 13, 9), runs=1))  # read after week A ended
        s.add(CollectionRun(source="stocktwits", week_start=wb, first_run_at=datetime(2026, 4, 13, 9),
                            last_run_at=datetime(2026, 4, 19, 20), runs=30))
    pipeline.process_week(Fixed(posts(80000, 6)), wa)
    pipeline.process_week(Fixed(posts(81000, 13)), wb)
    with session_scope() as s:
        m = s.scalar(select(WeeklyStockMetric).where(WeeklyStockMetric.week_start == wb,
                                                     WeeklyStockMetric.ticker == "SPY"))
        ov_a = s.scalar(select(WeeklyReport.overview).where(WeeklyReport.week_start == wa))
        ov_b = s.scalar(select(WeeklyReport.overview).where(WeeklyReport.week_start == wb))
    assert m.trend_class == "UNRATED" and m.mention_change_pct is None
    assert ov_a["text_coverage"]["complete"] is False and ov_b["text_coverage"]["complete"] is True
    assert ov_b["baseline_available"] is False


def test_stocktwits_week_without_run_record_is_partial(analysed_db):
    from app import pipeline
    from app.collectors.base import CollectedWeek
    w = date(2026, 3, 2)

    class Fixed:
        name, incremental = "stocktwits", False

        def collect(self, week):
            return CollectedWeek(week, posts=[parse_message(msg(90001, "2026-03-03T10:00:00Z", "$SPY"))],
                                 platform="stocktwits")
    pipeline.process_week(Fixed(), w)
    with session_scope() as s:
        assert pipeline.text_coverage(s, w)["complete"] is False
        assert pipeline.text_coverage(s, date(2025, 1, 6))["complete"] is True  # no StockTwits data -> complete
