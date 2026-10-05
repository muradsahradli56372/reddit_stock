"""End-to-end weekly pipeline.

  collect -> store raw (upsert) -> dedup -> extract -> disambiguate (ambiguous only)
          -> store mentions (insert-if-new) -> sentiment + reasons (only mentions not yet analysed)
          -> weekly metrics, trend + early-signal scores (replace for the week)
          -> prices (cached) + neutral attention-vs-price wording -> report + summary (upsert)

Each week is processed in ONE transaction, so a failure leaves the previous state intact.
Re-running a week is idempotent: raw data upserts on reddit_id, mentions are unique per
(company, source), metrics/report rows are replaced for that week.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from . import api_cache, attention, disambiguation, market_data, metrics, sentiment, summary
from .collectors.base import (CollectedWeek, Collector, last_complete_week, today_local, week_start_of,
                              week_window_utc)
from .collectors.demo import DemoCollector
from .config import settings
from .db import session_scope, upsert
from .extraction import Candidate, get_extractor
from .llm_cache import Cache
from .models import (AttentionSnapshot, Author, CollectionRun, Comment, Company, MentionReason, Post, StockMention, Subreddit,
                     WeeklyPrice,
                     WeeklyReport, WeeklyStockMetric)
from .reference import get_reference

log = logging.getLogger(__name__)
run_lock = threading.Lock()
BASELINE_WEEKS = 4


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def week_progress(week: date, now: datetime | None = None) -> float:
    """Fraction of the week that has elapsed (1.0 for finished weeks)."""
    start, end = week_window_utc(week)
    now = now or datetime.utcnow()
    if now >= end:
        return 1.0
    return max((now - start).total_seconds(), 0.0) / (end - start).total_seconds()


def purge_demo_data() -> dict:
    """Delete everything generated in demo mode so it never mixes with real data.
    Called on startup when a live text source is configured."""
    from .models import WeeklyPrice as WP
    with session_scope() as s:
        demo_post_ids = select(Post.id).where(Post.is_demo.is_(True))
        demo_comment_ids = select(Comment.id).where(Comment.is_demo.is_(True))
        demo_weeks = list(s.scalars(select(WeeklyReport.week_start).where(WeeklyReport.is_demo.is_(True))))
        n_mentions = s.execute(delete(StockMention).where(
            StockMention.post_id.in_(demo_post_ids) | StockMention.comment_id.in_(demo_comment_ids))).rowcount
        s.execute(delete(MentionReason).where(~MentionReason.mention_id.in_(select(StockMention.id))))
        n_comments = s.execute(delete(Comment).where(Comment.is_demo.is_(True))).rowcount
        n_posts = s.execute(delete(Post).where(Post.is_demo.is_(True))).rowcount
        s.execute(delete(WeeklyStockMetric).where(WeeklyStockMetric.week_start.in_(demo_weeks)))
        n_reports = s.execute(delete(WeeklyReport).where(WeeklyReport.is_demo.is_(True))).rowcount
        n_snap = s.execute(delete(AttentionSnapshot).where(AttentionSnapshot.source == "demo")).rowcount
        s.execute(delete(WP).where(WP.source == "demo"))
    out = {"posts": n_posts, "comments": n_comments, "mentions": n_mentions, "reports": n_reports,
           "snapshots": n_snap}
    if any(out.values()):
        log.info("demo data purged before live collection", extra=out)
    api_cache.clear()
    return out


def get_collector() -> Collector:
    """Text source per TEXT_SOURCE: demo | stocktwits | reddit (approval-gated Reddit Data API)."""
    src = settings.resolved_text_source
    if src == "stocktwits":
        from .collectors.stocktwits import StockTwitsCollector
        return StockTwitsCollector()
    if src == "reddit":
        from .collectors.reddit import RedditCollector
        return RedditCollector()
    return DemoCollector()


# ---------------------------------------------------------------- Reddit attention (count-only)
def store_snapshots(session: Session, provider_name: str, day: date, rows: list[attention.SnapshotRow]) -> int:
    """Upsert one day's counts; unknown tickers are added to `companies` (name from the source)."""
    if not rows:
        return 0
    known = set(session.scalars(select(Company.ticker)))
    new = {r.ticker: r.name for r in rows if r.ticker not in known}
    upsert(session, Company, [{"ticker": tk, "name": (name or tk)[:128], "exchange": "", "aliases": []}
                              for tk, name in new.items()], ["ticker"])
    now = datetime.utcnow()
    upsert(session, AttentionSnapshot, [{
        "source": provider_name, "community": r.community, "ticker": r.ticker, "snapshot_date": day,
        "mentions": r.mentions, "upvotes": r.upvotes, "rank": r.rank, "name": r.name, "captured_at": now,
    } for r in rows], ["source", "community", "ticker", "snapshot_date"],
        ["mentions", "upvotes", "rank", "name", "captured_at"])
    return len(rows)


def collect_attention(days: list[date]) -> dict:
    """Snapshot count-only attention. Live providers (ApeWisdom) can only snapshot today;
    the demo provider can backfill any day."""
    provider = attention.get_attention_provider()
    if provider is None:
        return {"provider": None, "rows": 0}
    if getattr(provider, "live_only", False):
        days = [today_local()]
    total = 0
    for d in days:
        try:
            rows = provider.snapshot(d)  # network I/O outside the transaction
        except Exception as exc:  # never break the run because the count source is down
            log.warning("attention snapshot failed", extra={"provider": provider.name, "error": repr(exc)})
            continue
        with session_scope() as session:
            total += store_snapshots(session, provider.name, d, rows)
    return {"provider": provider.name, "days": len(days),
            "from": days[0].isoformat() if days else None, "to": days[-1].isoformat() if days else None,
            "rows": total}


def load_reddit_week(session: Session, week: date) -> tuple[dict | None, int]:
    """({ticker: RedditWeek}, days_covered) or (None, 0) when the week has no snapshots."""
    rows = session.execute(
        select(AttentionSnapshot.community, AttentionSnapshot.ticker, AttentionSnapshot.snapshot_date,
               AttentionSnapshot.mentions, AttentionSnapshot.upvotes)
        .where(AttentionSnapshot.snapshot_date >= week,
               AttentionSnapshot.snapshot_date < week + timedelta(days=7))).all()
    if not rows:
        return None, 0
    return attention.aggregate_week([tuple(r) for r in rows])


def reddit_baselines(session: Session, week: date) -> tuple[dict | None, dict | None]:
    """(prev-week {ticker: mentions} or None, avg over up to 4 earlier COVERED weeks or None)."""
    prev, _ = load_reddit_week(session, week - timedelta(days=7))
    covered = []
    for i in range(1, 13):  # look back up to 12 weeks for 4 covered ones
        w, _ = load_reddit_week(session, week - timedelta(days=7 * i))
        if w is not None:
            covered.append(w)
        if len(covered) == BASELINE_WEEKS:
            break
    prev_map = {tk: rw.mentions for tk, rw in prev.items()} if prev is not None else None
    if not covered:
        return prev_map, None
    tickers = set().union(*[c.keys() for c in covered])
    avg = {tk: sum(c[tk].mentions for c in covered if tk in c) / len(covered) for tk in tickers}
    return prev_map, avg


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
    upsert(session, Subreddit, [{"name": s, "platform": data.platform} for s in {p.subreddit for p in data.posts}],
           ["name"], ["platform"])
    author_ids = dict(session.execute(select(Author.username, Author.id)).all())
    sub_ids = dict(session.execute(select(Subreddit.name, Subreddit.id)).all())
    _drop_mentions_of_edited(session, data)

    upsert(session, Post, [{
        "reddit_id": p.reddit_id, "subreddit_id": sub_ids[p.subreddit], "author_id": author_ids.get(p.author),
        "title": p.title, "body": p.body, "score": p.score, "num_comments": p.num_comments,
        "created_utc": p.created_utc, "week_start": week_start_of(p.created_utc),
        "content_hash": content_hash(p.title + "\n" + p.body), "is_demo": data.is_demo, "permalink": p.permalink,
        "author_sentiment": p.author_sentiment,
    } for p in data.posts], ["reddit_id"], ["title", "body", "score", "num_comments", "permalink", "content_hash",
                                            "author_sentiment"])

    post_ids = dict(session.execute(
        select(Post.reddit_id, Post.id).where(Post.reddit_id.in_({c.post_reddit_id for c in data.comments}))
    ).all()) if data.comments else {}
    upsert(session, Comment, [{
        "reddit_id": c.reddit_id, "post_id": post_ids[c.post_reddit_id], "author_id": author_ids.get(c.author),
        "body": c.body, "score": c.score, "created_utc": c.created_utc,
        "week_start": week_start_of(c.created_utc), "content_hash": content_hash(c.body), "is_demo": data.is_demo,
    } for c in data.comments if c.post_reddit_id in post_ids], ["reddit_id"], ["body", "score", "content_hash"])


def _drop_mentions_of_edited(session: Session, data: CollectedWeek) -> int:
    """If a stored post/comment's text changed (edited on Reddit), its old mentions are stale:
    delete them so they are re-extracted from the new text. Unchanged content keeps its mentions
    (and their already-paid-for sentiment)."""
    new_post = {p.reddit_id: content_hash(p.title + "\n" + p.body) for p in data.posts}
    new_comment = {c.reddit_id: content_hash(c.body) for c in data.comments}
    changed_posts, changed_comments = [], []
    ids = list(new_post)
    for i in range(0, len(ids), 500):
        for pid, rid, h in session.execute(select(Post.id, Post.reddit_id, Post.content_hash)
                                           .where(Post.reddit_id.in_(ids[i:i + 500]))).all():
            if h != new_post[rid]:
                changed_posts.append(pid)
    ids = list(new_comment)
    for i in range(0, len(ids), 500):
        for cid, rid, h in session.execute(select(Comment.id, Comment.reddit_id, Comment.content_hash)
                                           .where(Comment.reddit_id.in_(ids[i:i + 500]))).all():
            if h != new_comment[rid]:
                changed_comments.append(cid)
    n = 0
    for col, vals in ((StockMention.post_id, changed_posts), (StockMention.comment_id, changed_comments)):
        for i in range(0, len(vals), 500):
            n += session.execute(delete(StockMention).where(col.in_(vals[i:i + 500]))).rowcount or 0
    if n:
        log.info("edited content re-extracted", extra={"posts": len(changed_posts),
                                                        "comments": len(changed_comments), "mentions_dropped": n})
    return n


# ---------------------------------------------------------------- mentions
def process_mentions(session: Session, week: date) -> dict:
    """Extract mentions for every post/comment in the week. Returns stats."""
    extractor = get_extractor()
    company_ids = dict(session.execute(select(Company.ticker, Company.id)).all())

    posts = session.execute(
        select(Post.id, Post.title, Post.body, Post.author_id, Post.subreddit_id, Post.created_utc, Post.content_hash,
               Post.score)
        .where(Post.week_start == week)).all()
    comments = session.execute(
        select(Comment.id, Comment.body, Comment.author_id, Post.subreddit_id, Comment.created_utc,
               Comment.content_hash, Comment.score)
        .join(Post, Comment.post_id == Post.id).where(Comment.week_start == week)).all()

    sources = [("post", p.id, f"{p.title}\n\n{p.body}", p.author_id, p.subreddit_id, p.created_utc, p.content_hash,
                p.score) for p in posts]
    sources += [("comment", c.id, c.body, c.author_id, c.subreddit_id, c.created_utc, c.content_hash, c.score)
                for c in comments]
    sources.sort(key=lambda s: (s[5], s[0], s[1]))

    stats = {"sources": len(sources), "duplicates_skipped": 0, "accepted": 0, "ambiguous": 0,
             "ambiguous_accepted": 0, "ambiguous_rejected": 0, "blacklist_rejections": 0}
    seen: set[tuple[int, str]] = set()
    rows: list[dict] = []
    queue: list[tuple[Candidate, dict]] = []

    for kind, sid, text, author_id, sub_id, created, chash, score in sources:
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
                "author_id": author_id, "subreddit_id": sub_id, "week_start": week, "created_utc": created,
                "engagement": score}
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
    for (c, row), decision in zip(queue, disambiguation.resolve([c for c, _ in queue], Cache(session))):
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
    """Sentiment + reasons for mentions not analysed yet (one combined LLM call per batch)."""
    pending = session.execute(
        select(StockMention).where(StockMention.week_start == week,
                                   (StockMention.reasons_extracted.is_(None)) | (StockMention.sentiment.is_(None)))
    ).scalars().all()
    if not pending:
        return 0
    results = sentiment.classify([(m.ticker, m.context, m.matched_text) for m in pending], Cache(session))
    results = apply_author_labels(session, pending, results)
    session.execute(delete(MentionReason).where(MentionReason.mention_id.in_([m.id for m in pending])))
    reason_rows = []
    for m, r in zip(pending, results):
        m.sentiment, m.sentiment_confidence, m.sentiment_method = r.label, round(r.confidence, 2), r.method
        m.reasons_extracted = True
        for stance, cat in dict.fromkeys(r.reasons):  # de-duplicate, keep order
            reason_rows.append({"mention_id": m.id, "ticker": m.ticker, "week_start": week, "stance": stance,
                                "category": cat, "method": r.method})
    session.flush()
    upsert(session, MentionReason, reason_rows, ["mention_id", "category"])
    session.flush()
    return len(pending)


def apply_author_labels(session: Session, mentions: list, results: list) -> list:
    """StockTwits authors can tag a message Bullish/Bearish. When a message mentions exactly ONE
    ticker, that self-declared stance wins over our classifier (method="author_label"); reasons
    are kept only if they match the declared stance. Multi-ticker messages are left to the
    classifier, because the tag may refer to only one of the tickers."""
    post_ids = {m.post_id for m in mentions if m.post_id}
    if not post_ids:
        return results
    labels = dict(session.execute(select(Post.id, Post.author_sentiment)
                                  .where(Post.id.in_(post_ids), Post.author_sentiment.is_not(None))).all())
    if not labels:
        return results
    counts = dict(session.execute(select(StockMention.post_id, func.count())
                                  .where(StockMention.post_id.in_(list(labels)))
                                  .group_by(StockMention.post_id)).all())
    out = []
    for m, r in zip(mentions, results):
        label = labels.get(m.post_id)
        if label and counts.get(m.post_id) == 1:
            reasons_ = [x for x in r.reasons if x[0] == label] or \
                sentiment.reasons.fallback_reasons(m.context, label)
            r = sentiment.SentimentResult(label, 0.9, "author_label", reasons_)
        out.append(r)
    return out


def reasons_by_ticker(session: Session, week: date, tickers: list[str] | None = None) -> dict[str, dict]:
    """{ticker: {"bullish": [(category, count)], "bearish": [...]}} sorted by count."""
    q = (select(MentionReason.ticker, MentionReason.stance, MentionReason.category, func.count())
         .where(MentionReason.week_start == week)
         .group_by(MentionReason.ticker, MentionReason.stance, MentionReason.category))
    if tickers:
        q = q.where(MentionReason.ticker.in_(tickers))
    out: dict[str, dict] = {}
    for tk, stance, cat, n in session.execute(q).all():
        out.setdefault(tk, {"bullish": [], "bearish": []})[stance].append((cat, n))
    for d in out.values():
        for k in d:
            d[k].sort(key=lambda x: (-x[1], x[0]))
    return out


# ---------------------------------------------------------------- metrics
def load_mention_rows(session: Session, week: date) -> list[metrics.MentionRow]:
    q = (select(StockMention.ticker, StockMention.author_id, StockMention.source_type, Subreddit.name,
                StockMention.sentiment, StockMention.engagement)
         .join(Subreddit, StockMention.subreddit_id == Subreddit.id)
         .where(StockMention.week_start == week))
    return [metrics.MentionRow(t, str(a) if a is not None else None, st, sub, sen, eng or 0)
            for t, a, st, sub, sen, eng in session.execute(q).all()]


def record_collection_run(session: Session, source: str, week: date) -> None:
    now = datetime.utcnow()
    run = session.scalar(select(CollectionRun).where(CollectionRun.source == source,
                                                     CollectionRun.week_start == week))
    if run is None:
        session.add(CollectionRun(source=source, week_start=week, first_run_at=now, last_run_at=now, runs=1))
    else:
        run.last_run_at, run.runs = now, run.runs + 1
    session.flush()


def text_coverage(session: Session, week: date) -> dict:
    """Is this week's text sample complete enough to compare against?

    Incremental live sources (StockTwits) are complete only if collection started within the week's
    first day. Weeks collected in one go (demo, Reddit API) are complete by construction."""
    runs = session.scalars(select(CollectionRun).where(CollectionRun.week_start == week)).all()
    if not runs:
        # StockTwits data without a run record (collected by an older version): we can't tell when it
        # was read, so be conservative. Demo / Reddit-API weeks are collected in one go: complete.
        has_st = session.scalar(select(Post.id).where(Post.week_start == week, Post.reddit_id.like("st_%")).limit(1))
        return {"complete": has_st is None, "collected_from": None}
    start, _end = week_window_utc(week)
    first = min(r.first_run_at for r in runs)
    return {"complete": first <= start + timedelta(days=1), "collected_from": first.isoformat(timespec="minutes")}


def weeks_with_data(session: Session, before: date, limit: int) -> list[date]:
    q = (select(Post.week_start).distinct().where(Post.week_start < before)
         .order_by(Post.week_start.desc()).limit(limit))
    return list(session.execute(q).scalars().all())


def compute_week_metrics(session: Session, week: date) -> list[dict]:
    current_rows = load_mention_rows(session, week)
    current = metrics.aggregate_week(current_rows)
    # Only weeks whose text sample is complete can serve as a baseline.
    history_weeks = [w for w in weeks_with_data(session, week, BASELINE_WEEKS * 3)
                     if text_coverage(session, w)["complete"]][:BASELINE_WEEKS]
    history = [metrics.aggregate_week(load_mention_rows(session, w)) for w in history_weeks]
    baselines = metrics.build_baselines(history)
    progress = week_progress(week)
    if progress < 1.0:
        # Week in progress: compare with the previous weeks' PACE over the same elapsed share,
        # otherwise every stock would look like it is collapsing on Tuesday. (Reddit counts are
        # already per-day estimates scaled to 7 days, so they need no adjustment.)
        f = max(progress, 1 / 7)
        for b in baselines.values():
            b.prev_mentions = round(b.prev_mentions * f)
            b.prev_unique_authors = round(b.prev_unique_authors * f)
            b.prev_comment_mentions = round(b.prev_comment_mentions * f)
            b.avg_mentions *= f
            b.avg_unique_authors *= f
    n_subs = len({r.subreddit for r in current_rows}) or 1
    reddit, _days = load_reddit_week(session, week)
    reddit_prev, reddit_avg = reddit_baselines(session, week) if reddit is not None else (None, None)
    rows = metrics.build_metric_rows(current, baselines, n_subs, reddit, reddit_prev, reddit_avg,
                                     settings.reddit_min_weekly_mentions, text_baseline_known=bool(history))

    attach_prices(session, week, rows)
    company_ids = dict(session.execute(select(Company.ticker, Company.id)).all())
    session.execute(delete(WeeklyStockMetric).where(WeeklyStockMetric.week_start == week))
    session.add_all(WeeklyStockMetric(week_start=week, company_id=company_ids[r["ticker"]], **r) for r in rows)
    session.flush()
    return rows


def attention_change(row: dict, text_label: str) -> tuple[float | None, str]:
    """Which attention change to compare with price: Reddit counts when they have a previous week
    (complete volume), otherwise the text sample. Returns (change_pct, source label)."""
    if row.get("reddit_prev_mentions") is not None:
        return row["reddit_change_pct"], "Reddit"
    return row["mention_change_pct"], text_label


def attach_prices(session: Session, week: date, rows: list[dict]) -> None:
    """Price change for the top-N stocks (cached in weekly_prices) + neutral comparison sentence."""
    platforms = {p for (p,) in session.execute(
        select(Subreddit.platform).distinct().join(StockMention, StockMention.subreddit_id == Subreddit.id)
        .where(StockMention.week_start == week)).all()}
    text_label = "StockTwits" if platforms == {"stocktwits"} else "Reddit"
    for r in rows:
        r["price_change_pct"], r["attention_vs_price"] = None, None
    provider = market_data.get_provider()
    if provider is None or not rows:
        return
    top = [r["ticker"] for r in rows[:settings.market_data_top_n]]
    have = {p.ticker: p for p in session.scalars(
        select(WeeklyPrice).where(WeeklyPrice.week_start == week, WeeklyPrice.ticker.in_(top)))}
    missing = [t for t in top if t not in have]
    if missing:
        try:
            fetched = provider.weekly_changes(missing, week)
        except Exception as exc:  # no network, provider outage: report without prices
            log.warning("market data fetch failed", extra={"provider": provider.name, "error": repr(exc)})
            fetched = {}
        upsert(session, WeeklyPrice, [{
            "ticker": p.ticker, "week_start": week, "open_price": p.open_price, "close_price": p.close_price,
            "change_pct": p.change_pct, "source": p.source, "fetched_at": datetime.utcnow(),
        } for p in fetched.values()], ["ticker", "week_start"])
        session.flush()
        have.update({p.ticker: p for p in session.scalars(
            select(WeeklyPrice).where(WeeklyPrice.week_start == week, WeeklyPrice.ticker.in_(list(fetched))))})
    for r in rows:
        p = have.get(r["ticker"])
        if p is not None:
            r["price_change_pct"] = p.change_pct
            change, source = attention_change(r, text_label)
            r["attention_vs_price"] = market_data.describe_attention_vs_price(change, p.change_pct, source)


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
    reddit, days = load_reddit_week(session, week)
    reddit_overview = None
    if reddit is not None:
        comm_totals: dict[str, float] = {}
        for w in reddit.values():
            for c, v in w.distribution.items():
                comm_totals[c] = comm_totals.get(c, 0.0) + v
        reddit_overview = {
            "source": session.scalar(select(AttentionSnapshot.source).where(
                AttentionSnapshot.snapshot_date >= week,
                AttentionSnapshot.snapshot_date < week + timedelta(days=7)).limit(1)),
            "days_covered": days,
            "total_mentions": round(sum(w.mentions for w in reddit.values())),
            "tickers": len(reddit),
            "communities": {c: round(v) for c, v in sorted(comm_totals.items(), key=lambda x: -x[1])},
        }
    platforms = sorted({p or "reddit" for (p,) in session.execute(
        select(Subreddit.platform).distinct().join(StockMention, StockMention.subreddit_id == Subreddit.id)
        .where(StockMention.week_start == week)).all()})
    methods = {r for (r,) in session.execute(select(StockMention.sentiment_method).distinct()
                                             .where(StockMention.week_start == week)).all() if r}
    progress = week_progress(week)
    return {
        "week_start": week.isoformat(),
        "week_end": (week + timedelta(days=6)).isoformat(),
        "in_progress": progress < 1.0,
        "text_coverage": text_coverage(session, week),
        "baseline_available": any(r.get("has_baseline") for r in metric_rows),
        "unrated": sum(1 for r in metric_rows if r.get("trend_class") == "UNRATED"),
        "days_elapsed": round(progress * 7, 1),
        "posts": n_posts, "comments": n_comments,
        "total_mentions": len(mention_rows),
        "unique_authors": unique_authors,
        "mentioning_authors": mentioning_authors,
        "stocks_detected": len(metric_rows),
        "sentiment": metrics.sentiment_totals(mention_rows),
        "sentiment_methods": sorted(methods),
        "reddit_attention": reddit_overview,
        "text_platforms": platforms,
        "price_source": session.scalar(select(WeeklyPrice.source).where(WeeklyPrice.week_start == week).limit(1)),
        "subreddit_activity": subreddit_activity,
    }


# ---------------------------------------------------------------- orchestration
def should_collect(collector: Collector, week: date, now: datetime | None = None) -> bool:
    """Incremental live collectors (StockTwits) only read the open week, plus the week that just
    ended during the first day of a new week (to catch its last hours). Finished weeks are analysed
    from what was stored while they were open: paging back through days of newer messages would
    waste the rate limit and return a biased sample. Other collectors always collect."""
    if not getattr(collector, "incremental", False):
        return True
    now = now or datetime.utcnow()
    start, end = week_window_utc(week)
    return now < end + timedelta(days=1)


def process_week(collector: Collector, week: date) -> dict:
    t0 = time.monotonic()
    if should_collect(collector, week):
        data = collector.collect(week)  # network I/O happens outside the DB transaction
        if getattr(collector, "incremental", False):
            with session_scope() as s:
                record_collection_run(s, collector.name, week)
    else:
        data = CollectedWeek(week, platform=getattr(collector, "name", "reddit"))
    with session_scope() as session:
        seed_companies(session)
        store_raw(session, data)
        session.flush()
        stats = process_mentions(session, week)
        stats["sentiment_classified"] = classify_pending_sentiment(session, week)
        metric_rows = compute_week_metrics(session, week)
        overview = build_overview(session, week, metric_rows)
        if data.stats:
            overview["collection"] = data.stats
        movers = [r["ticker"] for r in metric_rows[:15]]
        text, method = summary.generate(overview, metric_rows, reasons_by_ticker(session, week, movers))
        upsert(session, WeeklyReport, [{
            "week_start": week, "overview": overview, "summary": text, "summary_method": method,
            "is_demo": data.is_demo or getattr(collector, "name", "") == "demo", "generated_at": datetime.utcnow(),
        }], ["week_start"], ["overview", "summary", "summary_method", "is_demo", "generated_at"])
        stats.update({"week_start": week.isoformat(), "posts": len(data.posts), "comments": len(data.comments),
                      "stocks": len(metric_rows), "summary_method": method,
                      "seconds": round(time.monotonic() - t0, 2)})
    log.info("week processed", extra=stats)
    return stats


def stocktwits_universe(session: Session, now_week: date) -> list[str]:
    """Which StockTwits symbol streams to read: Reddit's most-discussed and fastest-rising
    tickers (ApeWisdom, last 7 days) + StockTwits trending + the configured watchlist."""
    n = settings.stocktwits_universe_size
    since = datetime.utcnow().date() - timedelta(days=7)
    top = session.execute(
        select(AttentionSnapshot.ticker, func.sum(AttentionSnapshot.mentions).label("m"))
        .where(AttentionSnapshot.snapshot_date >= since)
        .group_by(AttentionSnapshot.ticker).order_by(func.sum(AttentionSnapshot.mentions).desc())
        .limit(n)).all()
    picked = [t for t, _ in top][: max(1, n * 2 // 3)]
    # risers: biggest increase of the latest snapshot vs. the average of the window
    latest = session.scalar(select(func.max(AttentionSnapshot.snapshot_date)))
    if latest:
        last = dict(session.execute(select(AttentionSnapshot.ticker, func.sum(AttentionSnapshot.mentions))
                                    .where(AttentionSnapshot.snapshot_date == latest)
                                    .group_by(AttentionSnapshot.ticker)).all())
        avg = dict(session.execute(select(AttentionSnapshot.ticker, func.avg(AttentionSnapshot.mentions))
                                   .where(AttentionSnapshot.snapshot_date >= since)
                                   .group_by(AttentionSnapshot.ticker)).all())
        risers = sorted(((last[t] / max(float(avg.get(t) or 1), 1.0), t) for t in last if last[t] >= 10),
                        reverse=True)
        picked += [t for _, t in risers]
    ordered = list(dict.fromkeys(picked + settings.stocktwits_watchlist))
    return ordered[: n + len(settings.stocktwits_watchlist)]


def prepare_collector(collector: Collector, week: date) -> None:
    """Give incremental collectors their universe and the ids already stored for the week."""
    if getattr(collector, "name", "") != "stocktwits":
        return
    with session_scope() as session:
        universe = stocktwits_universe(session, week)
        known = set(session.scalars(select(Post.reddit_id).where(Post.week_start == week,
                                                                 Post.reddit_id.like("st_%"))))
    trending = collector.trending() if hasattr(collector, "trending") else []
    collector.symbols = list(dict.fromkeys(universe + trending))
    collector.known_ids = known


def collect_now() -> dict:
    """Light job for the scheduler (COLLECT_CRON): snapshot Reddit attention for today and pull new
    text for the CURRENT week (stored raw; analysed when the week is complete)."""
    out = {"attention": collect_attention([])}
    collector = get_collector()
    if getattr(collector, "incremental", False):
        current = week_start_of(datetime.utcnow())
        out["text"] = {"source": collector.name, "new_items": 0, "weeks": []}
        for week in (current - timedelta(days=7), current):
            if not should_collect(collector, week):
                continue
            prepare_collector(collector, week)
            data = collector.collect(week)
            with session_scope() as session:
                store_raw(session, data)
                record_collection_run(session, collector.name, week)
            out["text"]["new_items"] += len(data.posts)
            out["text"]["weeks"].append(week.isoformat())
    log.info("collection finished", extra=out)
    return out


def run_analysis(week_start: date | None = None, collector: Collector | None = None) -> dict:
    """Process the target week and the week(s) before it, oldest first.

    Live: previous + target week (week-over-week needs both).
    Demo: DEMO_HISTORY_WEEKS weeks so history charts have data.
    """
    if not run_lock.acquire(blocking=False):
        raise RuntimeError("An analysis run is already in progress")
    try:
        collector = collector or get_collector()
        is_demo = getattr(collector, "name", "") == "demo"
        if week_start:
            target = week_start_of(week_start)
        elif is_demo or not getattr(collector, "incremental", False):
            target = last_complete_week()
        else:
            target = week_start_of(datetime.utcnow())  # live: the week in progress (+ the one before)
        n = max(2, settings.demo_history_weeks) if is_demo else 2
        weeks = [target - timedelta(days=7 * i) for i in range(n - 1, -1, -1)]
        log.info("analysis started", extra={"collector": getattr(collector, "name", "?"),
                                            "weeks": [w.isoformat() for w in weeks], "llm": settings.use_llm})
        att = collect_attention([weeks[0] + timedelta(days=i) for i in range(7 * len(weeks))])
        results = []
        for w in weeks:
            prepare_collector(collector, w)
            results.append(process_week(collector, w))
        return {"mode": "demo" if getattr(collector, "name", "") == "demo" else "live",
                "text_source": getattr(collector, "name", "?"), "attention": att,
                "llm": settings.use_llm, "weeks": results}
    finally:
        api_cache.clear()
        run_lock.release()
