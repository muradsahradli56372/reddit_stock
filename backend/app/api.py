"""HTTP API. All numbers come from the database; nothing is computed by an LLM here."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pipeline
from .collectors.base import week_start_of
from .config import settings
from .db import get_session
from .models import Company, StockMention, Subreddit, WeeklyReport, WeeklyStockMetric

router = APIRouter()

METRIC_FIELDS = (
    "rank", "ticker", "mentions", "unique_authors", "post_mentions", "comment_mentions",
    "bullish", "neutral", "bearish", "unclear", "bullish_pct", "neutral_pct", "bearish_pct",
    "subreddit_count", "subreddit_distribution", "top_author_share", "prev_mentions",
    "prev_unique_authors", "prev_comment_mentions", "mention_change_pct", "author_change_pct",
    "comment_change_pct", "trend_score", "trend_class",
)


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
    return {"status": "ok", "demo_mode": settings.demo_mode, "llm_enabled": settings.use_llm}


@router.get("/weeks")
def weeks(session: Session = Depends(get_session)):
    rows = session.execute(select(WeeklyReport.week_start, WeeklyReport.is_demo)
                           .order_by(WeeklyReport.week_start.desc())).all()
    return [{"week_start": w.isoformat(), "is_demo": d} for w, d in rows]


@router.get("/stocks")
def stocks(week: date | None = None, limit: int = Query(50, ge=1, le=500),
           sort: str = Query("mentions", pattern="^(mentions|trend_score|unique_authors|mention_change_pct)$"),
           session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    col = getattr(WeeklyStockMetric, sort)
    q = _metrics_query(wk).order_by(col.desc().nulls_last(), WeeklyStockMetric.ticker).limit(limit)
    return {"week_start": wk.isoformat(), "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]}


@router.get("/stocks/{ticker}")
def stock_detail(ticker: str, week: date | None = None, mentions_limit: int = Query(25, ge=0, le=200),
                 session: Session = Depends(get_session)):
    company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise HTTPException(404, f"Unknown ticker '{ticker.upper()}'")
    wk = resolve_week(session, week)
    m = session.scalar(select(WeeklyStockMetric).where(WeeklyStockMetric.week_start == wk,
                                                       WeeklyStockMetric.ticker == company.ticker))
    q = (select(StockMention, Subreddit.name).join(Subreddit, StockMention.subreddit_id == Subreddit.id)
         .where(StockMention.ticker == company.ticker, StockMention.week_start == wk)
         .order_by(StockMention.created_utc.desc()).limit(mentions_limit))
    mentions = [{
        "source_type": sm.source_type, "subreddit": sub, "created_utc": sm.created_utc.isoformat(),
        "detection_method": sm.detection_method, "confidence": sm.confidence, "matched_text": sm.matched_text,
        "context": sm.context, "sentiment": sm.sentiment, "sentiment_confidence": sm.sentiment_confidence,
        "sentiment_method": sm.sentiment_method,
    } for sm, sub in session.execute(q).all()]
    return {
        "ticker": company.ticker, "company": company.name, "exchange": company.exchange,
        "week_start": wk.isoformat(),
        "metrics": _metric_dict(m, company) if m else None,
        "recent_mentions": mentions,
    }


@router.get("/trending")
def trending(week: date | None = None, limit: int = Query(20, ge=1, le=200),
             min_growth: float | None = Query(None, description="Only stocks whose mentions grew more than this % WoW"),
             min_mentions: int = Query(0, ge=0), session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    q = _metrics_query(wk).where(WeeklyStockMetric.mentions >= min_mentions)
    if min_growth is not None:
        q = q.where(WeeklyStockMetric.mention_change_pct > min_growth)
    q = q.order_by(WeeklyStockMetric.trend_score.desc(), WeeklyStockMetric.ticker).limit(limit)
    return {"week_start": wk.isoformat(), "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]}


@router.get("/emerging")
def emerging(week: date | None = None, include_rising: bool = True, session: Session = Depends(get_session)):
    wk = resolve_week(session, week)
    classes = ["EMERGING", "RISING"] if include_rising else ["EMERGING"]
    q = (_metrics_query(wk).where(WeeklyStockMetric.trend_class.in_(classes))
         .order_by(WeeklyStockMetric.trend_score.desc()))
    return {"week_start": wk.isoformat(), "stocks": [_metric_dict(m, c) for m, c in session.execute(q).all()]}


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
        raise HTTPException(409, str(exc))
