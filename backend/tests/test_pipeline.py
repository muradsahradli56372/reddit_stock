from sqlalchemy import func, select

from app.collectors.demo import HEAVY_AUTHOR, DemoCollector
from app.db import session_scope
from app.models import (Author, Comment, Post, StockMention, WeeklyReport, WeeklyStockMetric)
from app.pipeline import run_analysis

from .conftest import ANCHOR, PREV


def counts():
    with session_scope() as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m))
                for m in (Author, Post, Comment, StockMention, WeeklyStockMetric, WeeklyReport)}


def metric(ticker, week=ANCHOR):
    with session_scope() as s:
        return s.scalar(select(WeeklyStockMetric).where(WeeklyStockMetric.week_start == week,
                                                        WeeklyStockMetric.ticker == ticker))


def test_both_weeks_processed(analysed_db):
    from app.config import settings
    weeks = [w["week_start"] for w in analysed_db["weeks"]]
    assert weeks[-2:] == [PREV.isoformat(), ANCHOR.isoformat()]
    assert len(weeks) == settings.demo_history_weeks  # demo mode builds history
    assert all(w["summary_method"] == "template" for w in analysed_db["weeks"])


def test_rerun_is_idempotent(analysed_db):
    before = counts()
    snapshot = {m.ticker: (m.mentions, m.unique_authors, m.trend_score) for m in
                [metric(t) for t in ("NVDA", "RKLB", "TSLA", "PLTR")]}
    run_analysis(ANCHOR, collector=DemoCollector(anchor_week=ANCHOR))
    assert counts() == before
    assert snapshot == {m.ticker: (m.mentions, m.unique_authors, m.trend_score) for m in
                        [metric(t) for t in ("NVDA", "RKLB", "TSLA", "PLTR")]}


def test_required_tickers_detected(analysed_db):
    for tk in ("NVDA", "TSLA", "PLTR", "AAPL", "RKLB", "ASTS", "SOFI"):
        m = metric(tk)
        assert m is not None and m.mentions > 0, tk


def test_false_positive_traps_not_counted(analysed_db):
    with session_scope() as s:
        tickers = set(s.scalars(select(StockMention.ticker).distinct()))
        # Bare AI/IT/ALL/A are blacklisted; DD/YOLO/CEO are not tickers at all.
        assert not tickers & {"AI", "IT", "ALL", "A", "DD", "YOLO", "CEO"}
        # "Ford" only ever appears as a person in the demo data.
        assert "F" not in tickers
        # "My coworker Sofi ..." must not be counted as SOFI.
        sofi_ctx = s.scalars(select(StockMention.context).where(StockMention.ticker == "SOFI")).all()
        assert sofi_ctx and not any("coworker Sofi" in c for c in sofi_ctx)
        apple_ctx = s.scalars(select(StockMention.context).where(StockMention.ticker == "AAPL")).all()
        assert not any("Apple pie" in c for c in apple_ctx)


def test_heavy_author_counts_once(analysed_db):
    with session_scope() as s:
        heavy_id = s.scalar(select(Author.id).where(Author.username == HEAVY_AUTHOR))
        heavy_mentions = s.scalar(select(func.count()).select_from(StockMention).where(
            StockMention.author_id == heavy_id, StockMention.week_start == ANCHOR, StockMention.ticker == "NVDA"))
    nvda = metric("NVDA")
    assert heavy_mentions >= 30
    assert nvda.unique_authors < nvda.mentions - heavy_mentions + 1 + 5  # heavy user adds only 1 author
    assert nvda.top_author_share >= heavy_mentions / nvda.mentions - 1e-3  # stored rounded to 3 dp


def test_same_author_duplicate_text_counted_once(analysed_db):
    assert analysed_db["weeks"][-1]["duplicates_skipped"] >= 4
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(StockMention).join(Comment, StockMention.comment_id == Comment.id)
                     .where(Comment.body == "NVDA to $500 by Christmas, mark my words 🚀",
                            StockMention.week_start == ANCHOR))
    assert n == 1


def test_week_over_week_consistent_with_previous_week(analysed_db):
    for tk in ("RKLB", "TSLA", "PLTR"):
        cur, prev = metric(tk), metric(tk, PREV)
        assert cur.prev_mentions == prev.mentions
        assert cur.mention_change_pct == round(100 * (cur.mentions - prev.mentions) / prev.mentions, 1)


def test_demo_trend_story(analysed_db):
    assert metric("RKLB").trend_class == "EMERGING"
    assert metric("ASTS").trend_class == "EMERGING"
    assert metric("TSLA").trend_class == "COOLING"
    assert metric("NVDA").trend_class == "STABLE"


def test_sentiment_stored_with_method(analysed_db):
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(StockMention).where(StockMention.sentiment.is_(None))) == 0
        methods = set(s.scalars(select(StockMention.sentiment_method).distinct()))
    assert methods == {"fallback_keywords"}


def test_report_sections_separated(analysed_db):
    with session_scope() as s:
        r = s.scalar(select(WeeklyReport).where(WeeklyReport.week_start == ANCHOR))
    assert r.summary_method == "template" and r.is_demo
    for key in ("data", "interpretation", "speculation", "disclaimer"):
        assert r.summary[key]
    assert r.overview["unique_authors"] > 0 and r.overview["total_mentions"] > 0


def test_reasons_stored_and_idempotent(analysed_db):
    from app.models import MentionReason, WeeklyPrice
    with session_scope() as s:
        n_reasons = s.scalar(select(func.count()).select_from(MentionReason))
        n_prices = s.scalar(select(func.count()).select_from(WeeklyPrice))
        pending = s.scalar(select(func.count()).select_from(StockMention)
                           .where(StockMention.reasons_extracted.is_(None)))
    assert n_reasons > 100 and n_prices > 0 and pending == 0
    run_analysis(ANCHOR, collector=DemoCollector(anchor_week=ANCHOR))
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(MentionReason)) == n_reasons
        assert s.scalar(select(func.count()).select_from(WeeklyPrice)) == n_prices


def test_early_signal_story(analysed_db):
    assert metric("RKLB").is_early_signal and metric("ASTS").is_early_signal
    assert not metric("NVDA").is_early_signal  # big and flat
    assert metric("NVDA").early_signal_score < metric("RKLB").early_signal_score


def test_engagement_and_prices_on_metrics(analysed_db):
    m = metric("PLTR")
    assert m.avg_engagement and m.avg_engagement > 0
    assert m.price_change_pct is not None and "co-occurrence" in m.attention_vs_price


def test_summary_mentions_reasons(analysed_db):
    with session_scope() as s:
        r = s.scalar(select(WeeklyReport).where(WeeklyReport.week_start == ANCHOR))
    assert any("discussion centres on" in line for line in r.summary["interpretation"])
    assert any("while the share price" in line for line in r.summary["data"])
    assert "no causal link" in r.summary["disclaimer"]
    assert r.overview["price_source"] == "demo"


def test_edited_content_is_reextracted(analysed_db):
    """A post whose text changed (edited on Reddit) loses stale mentions and gets fresh ones."""
    from datetime import datetime

    from app.collectors.base import CollectedWeek, RawPost
    from app.pipeline import process_week

    week = ANCHOR

    class OneShot:
        name = "test"

        def __init__(self, body):
            self.body = body

        def collect(self, w):
            return CollectedWeek(w, posts=[RawPost("edit_test_1", "stocks", "editor", "My pick", self.body, 5, 0,
                                                   datetime(2026, 9, 29, 12))])

    def tickers():
        with session_scope() as s:
            pid = s.scalar(select(Post.id).where(Post.reddit_id == "edit_test_1"))
            return set(s.scalars(select(StockMention.ticker).where(StockMention.post_id == pid)))

    process_week(OneShot("Loading up on AMD calls"), week)
    assert tickers() == {"AMD"}
    process_week(OneShot("Changed my mind, loading up on INTC calls"), week)
    assert tickers() == {"INTC"}
    process_week(OneShot("Changed my mind, loading up on INTC calls"), week)  # unchanged -> kept
    assert tickers() == {"INTC"}


def test_outdated_reports_detected(analysed_db):
    from app import pipeline
    assert pipeline.reports_outdated() is False
    with session_scope() as s:
        r = s.scalar(select(WeeklyReport).order_by(WeeklyReport.week_start.desc()).limit(1))
        r.overview = {k: v for k, v in r.overview.items() if k != "analysis_version"}
    assert pipeline.reports_outdated() is True
    run_analysis(ANCHOR, collector=DemoCollector(anchor_week=ANCHOR))
    assert pipeline.reports_outdated() is False
