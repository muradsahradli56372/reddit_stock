"""Collector interface. Phase 1 ships DemoCollector; Phase 2 adds a PRAW-based RedditCollector
that returns the same RawPost/RawComment structures, so nothing downstream changes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol


@dataclass
class RawPost:
    reddit_id: str
    subreddit: str
    author: str | None  # None = deleted
    title: str
    body: str
    score: int
    num_comments: int
    created_utc: datetime


@dataclass
class RawComment:
    reddit_id: str
    post_reddit_id: str
    author: str | None
    body: str
    score: int
    created_utc: datetime


@dataclass
class CollectedWeek:
    week_start: date
    posts: list[RawPost] = field(default_factory=list)
    comments: list[RawComment] = field(default_factory=list)
    is_demo: bool = False


class Collector(Protocol):
    name: str

    def collect(self, week_start: date) -> CollectedWeek: ...


def week_start_of(d: date | datetime) -> date:
    """ISO week start (Monday). All weeks are Monday 00:00 -> Sunday 23:59:59 UTC."""
    if isinstance(d, datetime):
        d = d.date()
    return d - timedelta(days=d.weekday())


def last_complete_week(today: date | None = None) -> date:
    today = today or datetime.utcnow().date()
    return week_start_of(today) - timedelta(days=7)
