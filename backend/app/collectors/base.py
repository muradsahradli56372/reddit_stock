"""Collector interface and week arithmetic.

DemoCollector and RedditCollector both return CollectedWeek, so nothing downstream cares
where data came from.

Weeks: Monday 00:00 -> Sunday 23:59:59 in settings.report_timezone (default UTC).
Timestamps are stored as naive UTC datetimes throughout the DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from ..config import settings


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
    permalink: str | None = None


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
    stats: dict = field(default_factory=dict)


class Collector(Protocol):
    name: str

    def collect(self, week_start: date) -> CollectedWeek: ...


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.report_timezone)


def week_start_of(d: date | datetime) -> date:
    """Monday of the week containing d. Naive datetimes are UTC and converted to the report tz."""
    if isinstance(d, datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d = d.astimezone(_tz()).date()
    return d - timedelta(days=d.weekday())


def week_window_utc(week_start: date) -> tuple[datetime, datetime]:
    """[start, end) of the week as naive UTC datetimes."""
    # Both ends are local midnights, so a DST change inside the week is handled correctly.
    start = datetime.combine(week_start, time(), tzinfo=_tz())
    end = datetime.combine(week_start + timedelta(days=7), time(), tzinfo=_tz())
    to_utc = lambda x: x.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return to_utc(start), to_utc(end)


def last_complete_week(now: datetime | None = None) -> date:
    """Monday of the most recent fully finished week (the previous 7 complete days)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return week_start_of(now.astimezone(timezone.utc).replace(tzinfo=None)) - timedelta(days=7)
