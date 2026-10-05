"""SQLAlchemy models.

Key design points:
- Reddit objects are keyed by their reddit_id (unique) so re-collection is an upsert.
- A stock mention is unique per (company, source_key) where source_key is
  "p:<reddit_id>" or "c:<reddit_id>": mentioning NVDA 3 times in one comment = 1 mention.
- weekly_stock_metrics is unique per (week_start, ticker); re-running a week replaces it.
- Indexes target the two hot queries:
    * all mentions for PLTR in week X           -> ix_mentions_ticker_week
    * stocks whose mentions grew > 100% WoW     -> ix_wsm_week_change
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Subreddit(Base):
    """A community where text was collected: a subreddit, or a whole platform such as 'stocktwits'.
    (Table name kept for backward compatibility.)"""
    __tablename__ = "subreddits"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    platform: Mapped[str | None] = mapped_column(String(16))  # reddit | stocktwits (NULL = reddit, pre-v0.3)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    reddit_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    subreddit_id: Mapped[int] = mapped_column(ForeignKey("subreddits.id"), nullable=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    num_comments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    permalink: Mapped[str | None] = mapped_column(String(512))  # Phase 2
    # Stance the AUTHOR declared (StockTwits "Bullish"/"Bearish" tag); NULL when none.
    author_sentiment: Mapped[str | None] = mapped_column(String(8))


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    reddit_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class StockMention(Base):
    __tablename__ = "stock_mentions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    source_type: Mapped[str] = mapped_column(String(8), nullable=False)  # "post" | "comment"
    source_key: Mapped[str] = mapped_column(String(40), nullable=False)  # "p:<id>" | "c:<id>"
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    comment_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    subreddit_id: Mapped[int] = mapped_column(ForeignKey("subreddits.id"), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    created_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    detection_method: Mapped[str] = mapped_column(String(24), nullable=False)  # cashtag|ticker|alias|disambiguated_*
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    matched_text: Mapped[str] = mapped_column(String(128), nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False)

    sentiment: Mapped[str | None] = mapped_column(String(10))  # bullish|neutral|bearish|unclear
    sentiment_confidence: Mapped[float | None] = mapped_column(Float)
    sentiment_method: Mapped[str | None] = mapped_column(String(24))  # llm|fallback_keywords
    reasons_extracted: Mapped[bool | None] = mapped_column(Boolean)  # Phase 2: NULL = not analysed yet
    engagement: Mapped[int | None] = mapped_column(Integer)  # score of the post/comment at collection
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "source_key", name="uq_mention_company_source"),
        Index("ix_mentions_ticker_week", "ticker", "week_start"),
        Index("ix_mentions_week", "week_start"),
    )


class WeeklyStockMetric(Base):
    __tablename__ = "weekly_stock_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)

    mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    unique_authors: Mapped[int] = mapped_column(Integer, nullable=False)
    post_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    comment_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    bullish: Mapped[int] = mapped_column(Integer, nullable=False)
    neutral: Mapped[int] = mapped_column(Integer, nullable=False)
    bearish: Mapped[int] = mapped_column(Integer, nullable=False)
    unclear: Mapped[int] = mapped_column(Integer, nullable=False)
    bullish_pct: Mapped[float] = mapped_column(Float, nullable=False)
    neutral_pct: Mapped[float] = mapped_column(Float, nullable=False)
    bearish_pct: Mapped[float] = mapped_column(Float, nullable=False)
    subreddit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    subreddit_distribution: Mapped[dict] = mapped_column(JSON, nullable=False)
    top_author_share: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    prev_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    prev_unique_authors: Mapped[int] = mapped_column(Integer, nullable=False)
    prev_comment_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    mention_change_pct: Mapped[float | None] = mapped_column(Float)  # NULL = new (no previous mentions)
    author_change_pct: Mapped[float | None] = mapped_column(Float)
    comment_change_pct: Mapped[float | None] = mapped_column(Float)

    trend_score: Mapped[float] = mapped_column(Float, nullable=False)
    trend_class: Mapped[str] = mapped_column(String(10), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)

    # Phase 2
    avg_engagement: Mapped[float | None] = mapped_column(Float)
    early_signal_score: Mapped[float | None] = mapped_column(Float)
    is_early_signal: Mapped[bool | None] = mapped_column(Boolean)
    price_change_pct: Mapped[float | None] = mapped_column(Float)
    attention_vs_price: Mapped[str | None] = mapped_column(Text)

    # v0.3: Reddit attention from count-only sources (ApeWisdom). NULL = no coverage that week.
    reddit_mentions: Mapped[float | None] = mapped_column(Float)  # estimated weekly mentions
    reddit_upvotes: Mapped[float | None] = mapped_column(Float)
    reddit_prev_mentions: Mapped[float | None] = mapped_column(Float)
    reddit_change_pct: Mapped[float | None] = mapped_column(Float)
    reddit_distribution: Mapped[dict | None] = mapped_column(JSON)  # {subreddit: est. mentions}
    reddit_days_covered: Mapped[int | None] = mapped_column(Integer)
    # False when there is no trustworthy earlier week to compare with (trend = UNRATED)
    has_baseline: Mapped[bool | None] = mapped_column(Boolean)
    text_baseline: Mapped[bool | None] = mapped_column(Boolean)  # text WoW comparable (else "–")

    __table_args__ = (
        UniqueConstraint("week_start", "ticker", name="uq_wsm_week_ticker"),
        Index("ix_wsm_week_change", "week_start", "mention_change_pct"),
        Index("ix_wsm_week_score", "week_start", "trend_score"),
        Index("ix_wsm_ticker_week", "ticker", "week_start"),
    )


class MentionReason(Base):
    """Structured reason behind a mention's stance, from a fixed taxonomy (see app/reasons.py)."""
    __tablename__ = "mention_reasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[int] = mapped_column(ForeignKey("stock_mentions.id", ondelete="CASCADE"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    stance: Mapped[str] = mapped_column(String(8), nullable=False)  # bullish | bearish
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(24), nullable=False)  # llm | fallback_keywords

    __table_args__ = (
        UniqueConstraint("mention_id", "category", name="uq_reason_mention_category"),
        Index("ix_reasons_ticker_week", "ticker", "week_start"),
    )


class WeeklyPrice(Base):
    """Cached weekly price change per ticker (from a MarketDataProvider)."""
    __tablename__ = "weekly_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    open_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # yfinance | demo
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("ticker", "week_start", name="uq_price_ticker_week"),)


class AttentionSnapshot(Base):
    """One day's mention count for a ticker in a community, from a count-only source (ApeWisdom).

    ApeWisdom reports rolling 24h counts and keeps no history, so we store one snapshot per
    (source, community, ticker, day); a later snapshot on the same day replaces the earlier one.
    Weekly mentions are estimated as mean(daily counts) * 7 over the days we have.
    """
    __tablename__ = "attention_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # apewisdom | demo
    community: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    upvotes: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(128))
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "community", "ticker", "snapshot_date", name="uq_snapshot"),
        Index("ix_snapshot_date", "snapshot_date"),
        Index("ix_snapshot_ticker_date", "ticker", "snapshot_date"),
    )


class CollectionRun(Base):
    """When an incremental collector (StockTwits) read a given week. A week's text sample is only
    trusted as a comparison baseline if collection started while the week was still open (within
    its first day); a week read only after it ended is a biased sample (busy symbols: last hours only)."""
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    first_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (UniqueConstraint("source", "week_start", name="uq_collection_run"),)


class LLMCache(Base):
    """Cache of per-item LLM answers keyed by sha256(task, model, input). Avoids paying twice."""
    __tablename__ = "llm_cache"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    overview: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)  # {data:[], interpretation:[], speculation:[]}
    summary_method: Mapped[str] = mapped_column(String(24), nullable=False)  # llm|template
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
