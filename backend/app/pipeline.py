"""End-to-end weekly pipeline.

  collect -> store raw (upsert) -> dedup -> extract -> disambiguate (ambiguous only)
          -> store mentions (insert-if-new) -> sentiment (only mentions without one)
          -> weekly metrics (replace for the week) -> report + summary (upsert)

Each week is processed in ONE transaction, so a failure leaves the previous state intact.
Re-running a week is idempotent: raw data upserts on reddit_id, mentions are unique per
(company, source), metrics/report rows are replaced for that week.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from . import disambiguation, metrics, sentiment, summary
from .collectors.base import CollectedWeek, Collector, last_complete_week, week_start_of
from .collectors.demo import DemoCollector
from .config import settings
from .db import session_scope, upsert
from .extraction import Candidate, get_extractor
from .models import (Author, Comment, Company, Post, StockMention, Subreddit, WeeklyReport,
                     WeeklyStockMetric)
from .reference import get_reference

log = logging.getLogger(__name__)
run_lock = threading.Lock()
BASELINE_WEEKS = 4


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def get_collector() -> Collector:
    # Phase 2: return RedditCollector() when settings.has_reddit_credentials and not settings.force_demo.
    return DemoCollector()


def seed_companies(session: Session) -> None:
    ref = get_reference()
    upsert(session, Company,
           [{"ticker": c.ticker, "name": c.name, "exchange": c.exchange, "aliases": list(c.aliases)}
            for c in ref.companies.values()],
           ["ticker"], ["name", "exchange", "aliases"])


# ---------------------------------------------------------------- raw storage
def store_raw(session: Session, data: CollectedWeek) -> None:
    names = {p.author for p in data.posts} | {c.author for c in data.comments}
    upsert(session, Author, [{"username": n, "is_deleted": False} for n in names if n], ["username"])
    upsert(session, Subreddit, [{"name": s} for s in {p.subreddit for p in data.posts}], ["name"])
    author_ids = dict(session.execute(select(Author.username, Author.id)).all())
    sub_ids = dict(session.execute(select(Subreddit.name, Subreddit.id)).all())

    upsert(session, Post, [{
        "reddit_id": p.reddit_id, "subreddit_id": sub_ids[p.subreddit], "author_id": author_ids.get(p.author),
        "title": p.title, "body": p.body, "score": p.score, "num_comments": p.num_comments,
        "created_utc": p.created_utc, "week_start": week_start_of(p.created_utc),
        "content_hash": content_hash(p.title + "\n" + p.body), "is_demo": data.is_demo,
    } for p in data.posts], ["reddit_id"], ["title", "body", "score", "num_comments"])

    post_ids = dict(session.execute(
        select(Post.reddit_id, Post.id).where(Post.reddit_id.in_({c.post_reddit_id for c in data.comments}))
    ).all()) if data.comments else {}
    upsert(session, Comment, [{
        "reddit_id": c.reddit_id, "post_id": post_ids[c.post_reddit_id], "author_id": author_ids.get(c.author),
        "body": c.body, "score": c.score, "created_utc": c.created_utc,
        "week_start": week_start_of(c.created_utc), "content_hash": content_hash(c.body), "is_demo": data.is_demo,
    } for c in data.comments if c.post_reddit_id in post_ids], ["reddit_id"], ["body", "score"])


# ---------------------------------------------------------------- mentions
def process_mentions(session: Session, week: date) -> dict:
    """Extract mentions for every post/comment in the week. Returns stats."""
    extractor = get_extractor()
    company_ids = dict(session.execute(select(Company.ticker, Company.id)).all())

    posts = session.execute(
        select(Post.id, Post.title, Post.body, Post.author_id, Post.subreddit_id, Post.created_utc, Post.content_hash)
        .where(Post.week_start == week)).all()
    comments = session.execute(
        select(Comment.id, Comment.body, Comment.author_id, Post.subreddit_id, Comment.created_utc,
               Comment.content_hash)
        .join(Post, Comment.post_id == Post.id).where(Comment.week_start == week)).all()

    sources = [("post", p.id, f"{p.title}\n\n{p.body}", p.author_id, p.subreddit_id, p.created_utc, p.content_hash)
               for p in posts]
    sources += [("comment", c.id, c.body, c.author_id, c.subreddit_id, c.created_utc, c.content_hash)
                for c in comments]
    sources.sort(key=lambda s: (s[5], s[0], s[1]))

    stats = {"sources": len(sources), "duplicates_skipped": 0, "accepted": 0, "ambiguous": 0,
             "ambiguous_accepted": 0, "ambiguous_rejected": 0, "blacklist_rejections": 0}
    seen: set[tuple[int, str]] = set()
    rows: list[dict] = []
    queue: list[tuple[Candidate, dict]] = []

    for kind, sid, text, author_id, sub_id, created, chash in sources:
        # Dedup: the same author posting identical text in the same week counts once.
        if author_id is not None:
            if (author_id, chash) in seen:
                stats["duplicates_skipped"] += 1
                continue
            seen.add((author_id, chash))
        res = extractor.extract(text)
        stats["blacklist_rejections"] += len(res.rejected)
        base = {"source_type": kind, "source_key": f"{kind[0]}:{sid}",
                "post_id": sid if kind == "post" else None, "comment_id": sid if kind == "comment" else None,
                "author_id": author_id, "subreddit_id": sub_id, "week_start": week, "created_utc": created}
        for c in res.candidates:
            if c.ticker not in company_ids:
                continue
            row = base | {"company_id": company_ids[c.ticker], "ticker": c.ticker, "detection_method": c.method,
                          "confidence": c.confidence, "matched_text": c.matched_text[:128], "context": c.context}
            if c.needs_disambiguation:
                queue.append((c, row))
            else:
                rows.append(row)

    # Only ask about ambiguous items that aren't already stored (saves LLM calls on re-runs).
    existing = set(session.execute(
        select(StockMention.company_id, StockMention.source_key).where(StockMention.week_start == week)).all())
    queue = [(c, r) for c, r in queue if (r["company_id"], r["source_key"]) not in existing]
    stats["ambiguous"] = len(queue)
    for (c, row), decision in zip(queue, disambiguation.resolve([c for c, _ in queue])):
        if decision.is_stock:
            stats["ambiguous_accepted"] += 1
            rows.append(row | {"detection_method": decision.method, "confidence": round(decision.confidence, 2)})
        else:
            stats["ambiguous_rejected"] += 1
    stats["accepted"] = len(rows)
    upsert(session, StockMention, rows, ["company_id", "source_key"])  # DO NOTHING on conflict
    session.flush()
    return stats


def classify_pending_sentiment(session: Session, week: date) -> int:
    pending = session.execute(
        select(StockMention).where(StockMention.week_start == week, StockMention.sentiment.is_(None))
    ).scalars().all()
    results = sentiment.classify([(m.ticker, m.context, m.matched_text) for m in pending])
    for m, r in zip(pending, results):
        m.sentiment, m.sentiment_confidence, m.sentiment_method = r.label, round(r.confidence, 2), r.method
    session.flush()
    return len(pending)


# ---------------------------------------------------------------- metrics
def load_mention_rows(session: Session, week: date) -> list[metrics.MentionRow]:
    q = (select(StockMention.ticker, StockMention.author_id, StockMention.source_type, Subreddit.name,
                StockMention.sentiment)
         .join(Subreddit, StockMention.subreddit_id == Subreddit.id)
         .where(StockMention.week_start == week))
    return [metrics.MentionRow(t, str(a) if a is not None else None, st, sub, sen)
            for t, a, st, sub, sen in session.execute(q).all()]


def weeks_with_data(session: Session, before: date, limit: int) -> list[date]:
    q = (select(Post.week_start).distinct().where(Post.week_start < before)
         .order_by(Post.week_start.desc()).limit(limit))
    return list(session.execute(q).scalars().all())


def compute_week_metrics(session: Session, week: date) -> list[dict]:
    current_rows = load_mention_rows(session, week)
    current = metrics.aggregate_week(current_rows)
    history = [metrics.aggregate_week(load_mention_rows(session, w))
               for w in weeks_with_data(session, week, BASELINE_WEEKS)]
    baselines = metrics.build_baselines(history)
    n_subs = len({r.subreddit for r in current_rows}) or 1
    rows = metrics.build_metric_rows(current, baselines, n_subs)

    company_ids = dict(session.execute(select(Company.ticker, Company.id)).all())
    session.execute(delete(WeeklyStockMetric).where(WeeklyStockMetric.week_start == week))
    session.add_all(WeeklyStockMetric(week_start=week, company_id=company_ids[r["ticker"]], **r) for r in rows)
    session.flush()
    return rows


def build_overview(session: Session, week: date, metric_rows: list[dict]) -> dict:
    mention_rows = load_mention_rows(session, week)
    n_posts = session.scalar(select(func.count()).select_from(Post).where(Post.week_start == week)) or 0
    n_comments = session.scalar(select(func.count()).select_from(Comment).where(Comment.week_start == week)) or 0
    post_authors = select(Post.author_id).where(Post.week_start == week, Post.author_id.is_not(None))
    comment_authors = select(Comment.author_id).where(Comment.week_start == week, Comment.author_id.is_not(None))
    active = post_authors.union(comment_authors).subquery()  # UNION already de-duplicates
    unique_authors = session.scalar(select(func.count()).select_from(active)) or 0
    mentioning_authors = len({r.author for r in mention_rows if r.author})

    sub_posts = dict(session.execute(
        select(Subreddit.name, func.count(Post.id)).join(Post, Post.subreddit_id == Subreddit.id)
        .where(Post.week_start == week).group_by(Subreddit.name)).all())
    sub_comments = dict(session.execute(
        select(Subreddit.name, func.count(Comment.id)).join(Post, Post.subreddit_id == Subreddit.id)
        .join(Comment, Comment.post_id == Post.id).where(Comment.week_start == week).group_by(Subreddit.name)).all())
    sub_mentions = metrics.subreddit_mentions(mention_rows)
    subreddit_activity = sorted(
        [{"subreddit": s, "posts": sub_posts.get(s, 0), "comments": sub_comments.get(s, 0),
          "mentions": sub_mentions.get(s, 0)} for s in set(sub_posts) | set(sub_comments)],
        key=lambda x: -x["mentions"])
    methods = {r for (r,) in session.execute(select(StockMention.sentiment_method).distinct()
                                             .where(StockMention.week_start == week)).all() if r}
    return {
        "week_start": week.isoformat(),
        "week_end": (week + timedelta(days=6)).isoformat(),
        "posts": n_posts, "comments": n_comments,
        "total_mentions": len(mention_rows),
        "unique_authors": unique_authors,
        "mentioning_authors": mentioning_authors,
        "stocks_detected": len(metric_rows),
        "sentiment": metrics.sentiment_totals(mention_rows),
        "sentiment_methods": sorted(methods),
        "subreddit_activity": subreddit_activity,
    }


# ---------------------------------------------------------------- orchestration
def process_week(collector: Collector, week: date) -> dict:
    with session_scope() as session:
        seed_companies(session)
        data = collector.collect(week)
        store_raw(session, data)
        session.flush()
        stats = process_mentions(session, week)
        stats["sentiment_classified"] = classify_pending_sentiment(session, week)
        metric_rows = compute_week_metrics(session, week)
        overview = build_overview(session, week, metric_rows)
        text, method = summary.generate(overview, metric_rows)
        upsert(session, WeeklyReport, [{
            "week_start": week, "overview": overview, "summary": text, "summary_method": method,
            "is_demo": data.is_demo, "generated_at": datetime.utcnow(),
        }], ["week_start"], ["overview", "summary", "summary_method", "is_demo", "generated_at"])
        stats.update({"week_start": week.isoformat(), "posts": len(data.posts), "comments": len(data.comments),
                      "stocks": len(metric_rows), "summary_method": method})
        return stats


def run_analysis(week_start: date | None = None, collector: Collector | None = None) -> dict:
    """Process the target week and the week before it (needed for week-over-week)."""
    if not run_lock.acquire(blocking=False):
        raise RuntimeError("An analysis run is already in progress")
    try:
        target = week_start_of(week_start) if week_start else last_complete_week()
        collector = collector or get_collector()
        weeks = [target - timedelta(days=7), target]
        results = [process_week(collector, w) for w in weeks]
        return {"mode": "demo" if settings.demo_mode else "live",
                "llm": settings.use_llm, "weeks": results}
    finally:
        run_lock.release()
