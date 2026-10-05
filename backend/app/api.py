"""HTTP API. All numbers come from the database; nothing is computed by an LLM here."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from . import api_cache, pipeline
from .collectors.base import week_start_of
from .config import settings
from .db import get_session
from .models import (Comment, Company, MentionReason, Post, StockMention, Subreddit, WeeklyPrice, WeeklyReport,
                     WeeklyStockMetric)
from .reasons import LABELS as REASON_LABELS

router = APIRouter()

METRIC_FIELDS = (
    "rank", "ticker", "mentions", "unique_authors", "post_mentions", "comment_mentions",
    "bullish", "neutral", "bearish", "unclear", "bullish_pct", "neutral_pct", "bearish_pct",
    "subreddit_count", "subreddit_distribution", "top_author_share", "prev_mentions",
    "prev_unique_authors", "prev_comment_mentions", "mention_change_pct", "author_change_pct",
    "comment_change_pct", "trend_score", "trend_class", "avg_engagement", "early_signal_score",
    "is_early_signal", "price_change_pct", "attention_vs_price",
    "reddit_mentions", "reddit_upvotes", "reddit_prev_mentions", "reddit_change_pct", "reddit_distribution",
    "reddit_days_covered",
)


def cached(request: Request, fn):
    """Serve GET responses from the in-process cache (cleared after each analysis run)."""
    return api_cache.get_or_set(str(request.url.path) + "?" + str(request.url.query), fn)


def _metric_dict(m: WeeklyStockMetric, company: Company) -> dict:
    d = {f: getattr(m, f) for f in METRIC_FIELDS}
    d.update(company=company.name, exchange=company.exchange, is_new=m.prev_mentions == 0,
             week_start=m.week_start.isoformat())
    return d


def resolve_week(session: Session, week: date | None) -> date:
    """Requested week normalised to its Monday, or the latest analysed week."""
    if week is not None:
        week = week_start_of(week)
        if session.scalar(select(WeeklyReport.id).where(WeeklyReport.week_start == week)) is None:
            raise HTTPException(404, f"No analysis for week starting {week.isoformat()}. Run POST /analysis/run.")
        return week
    latest = session.scalar(select(WeeklyReport.week_start).order_by(WeeklyReport.week_start.desc()).limit(1))
    if latest is None:
        raise HTTPException(404, "No analysis has been run yet. Call POST /analysis/run.")
    return latest


def _metrics_query(week: date):
    return (select(WeeklyStockMetric, Company).join(Company, WeeklyStockMetric.company_id == Company.id)
            .where(WeeklyStockMetric.week_start == week))


@router.get("/health")
def health():
    from .market_data import get_provider
    provider = get_provider()
    return {"status": "ok", "demo_mode": settings.demo_mode, "llm_enabled": settings.use_llm,
            "text_source": settings.resolved_text_source, "attention_source": settings.resolved_attention_source,
            "market_data": provider.name if provider else None, "timezone": settings.report_timezone,
            "scheduler": {"enabled": settings.scheduler_enabled, "cron": settings.schedule_cron,
                          "collect_cron": settings.collect_cron},
            "cache": dict(api_cache.stats)}


@router.get("/weeks")
def weeks(session: Session = Depends(get_session)):
    rows = session.execute(select(WeeklyReport.week_start, WeeklyReport.is_demo)
                           .order_by(WeeklyReport.week_start.desc())).all()
    return [{"week_start": w.isoformat(), "is_demo": d} for w, d in rows]


@router.get("/stocks")
def stocks(request: Request, week: date | None = None, limit: int = Query(50, ge=1, le=500),
           sort: str = Query("rank", pattern="^(rank|mentions|trend_score|unique_authors|mention_change_pct|"
                                              "early_signal_score|reddit_mentions|reddit_change_pct)$"),
           session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    col = getattr(WeeklyStockMetric, sort)
    order = col.asc() if sort == "rank" else col.desc().nulls_last()
    q = _metrics_query(wk).order_by(order, WeeklyStockMetric.ticker).limit(limit)
    return cached(request, lambda: {"week_start": wk.isoformat(),
                                    "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]})


def _company_or_404(session: Session, ticker: str) -> Company:
    company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise HTTPException(404, f"Unknown ticker '{ticker.upper()}'")
    return company


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def stock_reasons(session: Session, ticker: str, week: date) -> dict:
    rows = session.execute(
        select(MentionReason.stance, MentionReason.category, func.count())
        .where(MentionReason.ticker == ticker, MentionReason.week_start == week)
        .group_by(MentionReason.stance, MentionReason.category)).all()
    out = {"bullish": [], "bearish": []}
    for stance, cat, n in rows:
        out[stance].append({"category": cat, "label": REASON_LABELS.get(cat, cat), "count": n})
    for v in out.values():
        v.sort(key=lambda r: (-r["count"], r["category"]))
    methods = session.scalars(select(MentionReason.method).distinct().where(
        MentionReason.ticker == ticker, MentionReason.week_start == week)).all()
    out["methods"] = sorted(methods)
    return out


def subreddit_sentiment(session: Session, ticker: str, week: date) -> list[dict]:
    rows = session.execute(
        select(Subreddit.name, StockMention.sentiment, func.count(), func.count(func.distinct(StockMention.author_id)))
        .join(Subreddit, StockMention.subreddit_id == Subreddit.id)
        .where(StockMention.ticker == ticker, StockMention.week_start == week)
        .group_by(Subreddit.name, StockMention.sentiment)).all()
    agg: dict[str, dict] = defaultdict(lambda: {"bullish": 0, "neutral": 0, "bearish": 0, "unclear": 0})
    for sub, sent, n, _a in rows:
        agg[sub][sent or "unclear"] += n
    out = []
    for sub, c in agg.items():
        total = sum(c.values())
        out.append({"subreddit": sub, "mentions": total, **c,
                    "bullish_pct": _pct(c["bullish"], total), "bearish_pct": _pct(c["bearish"], total),
                    "net_sentiment": round(_pct(c["bullish"], total) - _pct(c["bearish"], total), 1)})
    return sorted(out, key=lambda r: -r["mentions"])


def top_posts(session: Session, ticker: str, week: date, limit: int = 5) -> list[dict]:
    """Threads discussing the ticker: posts that mention it directly first, then by mentions in the thread."""
    thread_post = func.coalesce(StockMention.post_id, Comment.post_id)
    per_thread = (select(thread_post.label("pid"), func.count().label("n"),
                         func.max(case((StockMention.source_type == "post", 1), else_=0)).label("direct"))
                  .select_from(StockMention).outerjoin(Comment, StockMention.comment_id == Comment.id)
                  .where(StockMention.ticker == ticker, StockMention.week_start == week)
                  .group_by(thread_post).subquery())
    q = (select(Post, Subreddit.name, per_thread.c.n).join(per_thread, Post.id == per_thread.c.pid)
         .join(Subreddit, Post.subreddit_id == Subreddit.id)
         .order_by(per_thread.c.direct.desc(), per_thread.c.n.desc(), Post.score.desc()).limit(limit))
    out = []
    for p, sub, n in session.execute(q).all():
        sent = session.scalar(select(StockMention.sentiment).where(StockMention.post_id == p.id,
                                                                   StockMention.ticker == ticker))
        out.append({"title": p.title, "subreddit": sub, "score": p.score, "num_comments": p.num_comments,
                    "mentions_in_thread": n, "permalink": p.permalink, "created_utc": p.created_utc.isoformat(),
                    "post_sentiment": sent, "is_demo": p.is_demo})
    return out


@router.get("/stocks/{ticker}")
def stock_detail(request: Request, ticker: str, week: date | None = None,
                 mentions_limit: int = Query(25, ge=0, le=200), session: Session = Depends(get_session)):
    company = _company_or_404(session, ticker)
    wk = resolve_week(session, week)

    def build():
        m = session.scalar(select(WeeklyStockMetric).where(WeeklyStockMetric.week_start == wk,
                                                           WeeklyStockMetric.ticker == company.ticker))
        q = (select(StockMention, Subreddit.name).join(Subreddit, StockMention.subreddit_id == Subreddit.id)
             .where(StockMention.ticker == company.ticker, StockMention.week_start == wk)
             .order_by(StockMention.created_utc.desc()).limit(mentions_limit))
        mentions = [{
            "source_type": sm.source_type, "subreddit": sub, "created_utc": sm.created_utc.isoformat(),
            "detection_method": sm.detection_method, "confidence": sm.confidence, "matched_text": sm.matched_text,
            "context": sm.context, "sentiment": sm.sentiment, "sentiment_confidence": sm.sentiment_confidence,
            "sentiment_method": sm.sentiment_method, "engagement": sm.engagement,
        } for sm, sub in session.execute(q).all()]
        price = session.scalar(select(WeeklyPrice).where(WeeklyPrice.week_start == wk,
                                                         WeeklyPrice.ticker == company.ticker))
        return {
            "ticker": company.ticker, "company": company.name, "exchange": company.exchange,
            "week_start": wk.isoformat(),
            "metrics": _metric_dict(m, company) if m else None,
            "reasons": stock_reasons(session, company.ticker, wk),
            "subreddit_sentiment": subreddit_sentiment(session, company.ticker, wk),
            "top_posts": top_posts(session, company.ticker, wk),
            "price": {"change_pct": price.change_pct, "open": price.open_price, "close": price.close_price,
                      "source": price.source,
                      "attention_vs_price": m.attention_vs_price if m else None} if price else None,
            "recent_mentions": mentions,
        }
    return cached(request, build)


@router.get("/stocks/{ticker}/history")
def stock_history(request: Request, ticker: str, weeks: int = Query(12, ge=1, le=104),
                  session: Session = Depends(get_session)):
    company = _company_or_404(session, ticker)

    def build():
        all_weeks = session.scalars(select(WeeklyReport.week_start)
                                    .order_by(WeeklyReport.week_start.desc()).limit(weeks)).all()[::-1]
        rows = {m.week_start: m for m in session.scalars(
            select(WeeklyStockMetric).where(WeeklyStockMetric.ticker == company.ticker,
                                            WeeklyStockMetric.week_start.in_(all_weeks)))}
        prices = {p.week_start: p for p in session.scalars(
            select(WeeklyPrice).where(WeeklyPrice.ticker == company.ticker, WeeklyPrice.week_start.in_(all_weeks)))}
        series = []
        for w in all_weeks:
            m, p = rows.get(w), prices.get(w)
            series.append({
                "week_start": w.isoformat(),
                "mentions": m.mentions if m else 0, "unique_authors": m.unique_authors if m else 0,
                "bullish": m.bullish if m else 0, "neutral": m.neutral if m else 0,
                "bearish": m.bearish if m else 0, "unclear": m.unclear if m else 0,
                "bullish_pct": m.bullish_pct if m else 0.0, "neutral_pct": m.neutral_pct if m else 0.0,
                "bearish_pct": m.bearish_pct if m else 0.0,
                "mention_change_pct": m.mention_change_pct if m else None,
                "trend_score": m.trend_score if m else None, "trend_class": m.trend_class if m else None,
                "early_signal_score": m.early_signal_score if m else None,
                "reddit_mentions": m.reddit_mentions if m else None,
                "reddit_change_pct": m.reddit_change_pct if m else None,
                "price_change_pct": p.change_pct if p else None, "price_close": p.close_price if p else None,
                "price_source": p.source if p else None,
            })
        return {"ticker": company.ticker, "company": company.name, "weeks": series}
    return cached(request, build)


@router.get("/trending")
def trending(request: Request, week: date | None = None, limit: int = Query(20, ge=1, le=200),
             min_growth: float | None = Query(None, description="Only stocks whose mentions grew more than this % WoW"),
             min_mentions: int = Query(0, ge=0), session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    q = _metrics_query(wk).where(WeeklyStockMetric.mentions >= min_mentions)
    if min_growth is not None:
        q = q.where(WeeklyStockMetric.mention_change_pct > min_growth)
    q = q.order_by(WeeklyStockMetric.trend_score.desc(), WeeklyStockMetric.ticker).limit(limit)
    return cached(request, lambda: {"week_start": wk.isoformat(),
                                    "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]})


@router.get("/emerging")
def emerging(week: date | None = None, include_rising: bool = True, session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    classes = ["EMERGING", "RISING"] if include_rising else ["EMERGING"]
    q = (_metrics_query(wk).where(WeeklyStockMetric.trend_class.in_(classes))
         .order_by(WeeklyStockMetric.trend_score.desc()))
    return {"week_start": wk.isoformat(), "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]}


@router.get("/early-signals")
def early_signals(request: Request, week: date | None = None, include_all: bool = False,
                  limit: int = Query(20, ge=1, le=200), session: Session = Depends(get_session)):
    """Small stocks whose discussion is broadening fast (see app/signals.py for the formula)."""
    wk = resolve_week(session, week)
    q = _metrics_query(wk).where(WeeklyStockMetric.early_signal_score > 0)
    if not include_all:
        q = q.where(WeeklyStockMetric.is_early_signal.is_(True))
    q = q.order_by(WeeklyStockMetric.early_signal_score.desc(), WeeklyStockMetric.ticker).limit(limit)
    return cached(request, lambda: {"week_start": wk.isoformat(),
                                    "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]})


@router.get("/subreddits")
def subreddits(request: Request, week: date | None = None, session: Session = Depends(get_session)):
    """Per-subreddit activity, sentiment and the tickers each one is talking about."""
    wk = resolve_week(session, week)

    def build():
        report = session.scalar(select(WeeklyReport).where(WeeklyReport.week_start == wk))
        activity = {a["subreddit"]: a for a in report.overview.get("subreddit_activity", [])}
        rows = session.execute(
            select(Subreddit.name, StockMention.ticker, StockMention.sentiment, func.count())
            .join(Subreddit, StockMention.subreddit_id == Subreddit.id)
            .where(StockMention.week_start == wk)
            .group_by(Subreddit.name, StockMention.ticker, StockMention.sentiment)).all()
        authors = dict(session.execute(
            select(Subreddit.name, func.count(func.distinct(StockMention.author_id)))
            .join(Subreddit, StockMention.subreddit_id == Subreddit.id)
            .where(StockMention.week_start == wk).group_by(Subreddit.name)).all())
        tick: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        sent: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for sub, tk, s_, n in rows:
            tick[sub][tk] += n
            sent[sub][s_ or "unclear"] += n
        out = []
        for sub in sorted(set(activity) | set(tick)):
            total = sum(sent[sub].values())
            a = activity.get(sub, {})
            out.append({
                "subreddit": sub, "posts": a.get("posts", 0), "comments": a.get("comments", 0), "mentions": total,
                "mentioning_authors": authors.get(sub, 0),
                "bullish_pct": _pct(sent[sub]["bullish"], total), "bearish_pct": _pct(sent[sub]["bearish"], total),
                "neutral_pct": _pct(sent[sub]["neutral"], total),
                "top_tickers": [{"ticker": t, "mentions": n} for t, n in
                                sorted(tick[sub].items(), key=lambda x: (-x[1], x[0]))[:5]],
            })
        out.sort(key=lambda r: -r["mentions"])
        return {"week_start": wk.isoformat(), "subreddits": out,
                "reddit_communities": reddit_communities(session, wk)}
    return cached(request, build)


def reddit_communities(session: Session, week: date) -> list[dict]:
    """Count-only Reddit attention per community (ApeWisdom filter) with its top tickers."""
    reddit, days = pipeline.load_reddit_week(session, week)
    if reddit is None:
        return []
    per: dict[str, dict[str, float]] = defaultdict(dict)
    for tk, w in reddit.items():
        for c, v in w.distribution.items():
            per[c][tk] = v
    out = [{"community": c, "mentions": round(sum(t.values())), "days_covered": days,
            "top_tickers": [{"ticker": tk, "mentions": round(v)} for tk, v in
                            sorted(t.items(), key=lambda x: (-x[1], x[0]))[:5]]}
           for c, t in per.items()]
    return sorted(out, key=lambda r: -r["mentions"])


@router.post("/collect/run")
def collect_run():
    """Run the light collection job now (Reddit attention snapshot + new StockTwits messages)."""
    try:
        return pipeline.collect_now()
    finally:
        api_cache.clear()


@router.get("/sentiment")
def sentiment_view(week: date | None = None, session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    report = session.scalar(select(WeeklyReport).where(WeeklyReport.week_start == wk))
    q = _metrics_query(wk).order_by(WeeklyStockMetric.mentions.desc())
    per_stock = [{k: d[k] for k in ("ticker", "mentions", "bullish", "neutral", "bearish", "unclear",
                                    "bullish_pct", "neutral_pct", "bearish_pct")}
                 for d in (_metric_dict(m, c) for m, c in session.execute(q).all())]
    return {"week_start": wk.isoformat(), "overall": report.overview["sentiment"],
            "methods": report.overview.get("sentiment_methods", []), "stocks": per_stock}


@router.get("/weekly-report")
def weekly_report(week: date | None = None, session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    r = session.scalar(select(WeeklyReport).where(WeeklyReport.week_start == wk))
    return {"week_start": wk.isoformat(), "overview": r.overview, "summary": r.summary,
            "summary_method": r.summary_method, "is_demo": r.is_demo, "generated_at": r.generated_at.isoformat()}


class RunRequest(BaseModel):
    week_start: date | None = None


@router.post("/analysis/run")
def run(req: RunRequest | None = None):
    week = req.week_start if req else None
    if week is not None and week > date.today():
        raise HTTPException(422, "week_start cannot be in the future")
    try:
        return pipeline.run_analysis(week)
    except RuntimeError as exc:
        if "already in progress" in str(exc):
            raise HTTPException(409, str(exc))
        raise
