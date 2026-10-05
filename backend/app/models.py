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
    __tablename__ = "subreddits"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


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

    __table_args__ = (
        UniqueConstraint("week_start", "ticker", name="uq_wsm_week_ticker"),
        Index("ix_wsm_week_change", "week_start", "mention_change_pct"),
        Index("ix_wsm_week_score", "week_start", "trend_score"),
    )


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    overview: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)  # {data:[], interpretation:[], speculation:[]}
    summary_method: Mapped[str] = mapped_column(String(24), nullable=False)  # llm|template
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
