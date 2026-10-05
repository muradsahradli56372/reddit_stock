"""Live Reddit collector (PRAW).

What it does for one week [Mon 00:00, next Mon 00:00) in REPORT_TIMEZONE:
  * For each configured subreddit, page through /new (PRAW paginates 100 at a time) until
    posts are older than the window, and also /top?t=week|month so popular posts in the window
    aren't missed. Merged by id.
  * For each post in the window: expand comments (replace_more up to REDDIT_REPLACE_MORE_LIMIT)
    and keep comments created inside the window.
  * Deleted accounts -> author None; "[deleted]"/"[removed]" bodies are dropped (comments) or
    blanked (post bodies; the title remains).
  * Rate limits: PRAW reads Reddit's rate-limit headers and sleeps as needed (OAuth: ~100
    requests/min). On top of that every network call is wrapped in retries with exponential
    backoff for 429 / 5xx / connection errors.

Reddit's limits (important):
  * Listings return at most ~1000 items. Very busy subreddits (r/wallstreetbets) can exceed
    1000 posts/week, so /new may not reach back to Monday; we log `window_reached=False`.
  * There is no official historical archive API: collecting a week long after it ended
    returns whatever is still in the listings. Run weekly (the scheduler does) for best coverage.
  * Comments on posts created BEFORE the window (e.g. last week's threads) are not collected.
  * Scores are as of collection time, not at end of week.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, TypeVar

from ..config import settings
from .base import CollectedWeek, RawComment, RawPost, week_window_utc

log = logging.getLogger(__name__)
T = TypeVar("T")

DELETED_BODIES = {"[deleted]", "[removed]", "[ Removed by Reddit ]", ""}


def _retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {"TooManyRequests", "ServerError", "RequestException", "ConnectionError", "Timeout",
                "ReadTimeout", "ConnectTimeout"}:
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status is not None and (status == 429 or status >= 500)


def with_retries(fn: Callable[[], T], what: str, attempts: int | None = None,
                 base_delay: float = 2.0, sleep: Callable[[float], Any] = time.sleep) -> T:
    """Call fn(); on retryable errors wait base*2^n seconds (2, 4, 8, ...) and try again."""
    attempts = attempts or settings.reddit_max_retries
    for n in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _retryable(exc) or n == attempts - 1:
                raise
            delay = base_delay * (2 ** n)
            log.warning("reddit call failed, retrying", extra={"what": what, "attempt": n + 1,
                                                               "delay_s": delay, "error": repr(exc)})
            sleep(delay)
    raise RuntimeError("unreachable")


def _author(obj) -> str | None:
    a = getattr(obj, "author", None)
    return None if a is None else str(getattr(a, "name", a))


def _ts(obj) -> datetime:
    return datetime.fromtimestamp(float(obj.created_utc), tz=timezone.utc).replace(tzinfo=None)


class RedditCollector:
    name = "reddit"

    def __init__(self, reddit=None, subreddits: list[str] | None = None, sleep=time.sleep):
        self.subreddits = subreddits or settings.subreddits
        self._reddit = reddit
        self._sleep = sleep

    @property
    def reddit(self):
        if self._reddit is None:
            import praw
            self._reddit = praw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                user_agent=settings.reddit_user_agent,
                ratelimit_seconds=300,  # let PRAW sleep up to 5 min on rate limits instead of failing
            )
            self._reddit.read_only = True
        return self._reddit

    def _retry(self, fn, what):
        return with_retries(fn, what, sleep=self._sleep)

    def _listing(self, gen, start: datetime, stop_when_older: bool) -> tuple[list, bool]:
        """Iterate a PRAW listing generator with retries; returns (items, reached_window_start)."""
        items, reached = [], False
        it = iter(gen)
        while len(items) < settings.reddit_max_posts_per_sub:
            try:
                sub = self._retry(lambda: next(it), "listing.next")
            except StopIteration:
                reached = True  # listing exhausted
                break
            items.append(sub)
            if stop_when_older and _ts(sub) < start and not getattr(sub, "stickied", False):
                reached = True
                break
        return items, reached

    def collect(self, week_start: date) -> CollectedWeek:
        start, end = week_window_utc(week_start)
        out = CollectedWeek(week_start=week_start, is_demo=False, stats={"subreddits": {}})
        seen_posts: set[str] = set()

        for name in self.subreddits:
            t0 = time.monotonic()
            sub_stats = {"posts": 0, "comments": 0, "deleted_skipped": 0, "window_reached": False, "error": None}
            try:
                sr = self.reddit.subreddit(name)
                new_items, reached = self._listing(sr.new(limit=None), start, stop_when_older=True)
                top_items, _ = self._listing(sr.top(time_filter="month", limit=100), start, stop_when_older=False)
                sub_stats["window_reached"] = reached
                candidates = {s.id: s for s in new_items + top_items}

                for s in candidates.values():
                    created = _ts(s)
                    if not (start <= created < end) or s.id in seen_posts:
                        continue
                    seen_posts.add(s.id)
                    body = s.selftext or ""
                    if body.strip() in DELETED_BODIES:
                        body = ""
                    out.posts.append(RawPost(
                        reddit_id=f"t3_{s.id}", subreddit=name, author=_author(s), title=s.title or "",
                        body=body, score=int(s.score or 0), num_comments=int(s.num_comments or 0),
                        created_utc=created, permalink=f"https://www.reddit.com{s.permalink}"
                        if getattr(s, "permalink", None) else None))
                    sub_stats["posts"] += 1

                    def load_comments(s=s):
                        s.comments.replace_more(limit=settings.reddit_replace_more_limit)
                        return s.comments.list()
                    for c in self._retry(load_comments, f"comments:{s.id}"):
                        body = (getattr(c, "body", "") or "").strip()
                        if body in DELETED_BODIES:
                            sub_stats["deleted_skipped"] += 1
                            continue
                        c_created = _ts(c)
                        if not (start <= c_created < end):
                            continue
                        out.comments.append(RawComment(
                            reddit_id=f"t1_{c.id}", post_reddit_id=f"t3_{s.id}", author=_author(c),
                            body=body, score=int(getattr(c, "score", 0) or 0), created_utc=c_created))
                        sub_stats["comments"] += 1
            except Exception as exc:  # one bad subreddit (private, banned, outage) must not kill the run
                sub_stats["error"] = repr(exc)
                log.error("subreddit collection failed", extra={"subreddit": name, "error": repr(exc)})
            sub_stats["seconds"] = round(time.monotonic() - t0, 1)
            out.stats["subreddits"][name] = sub_stats
            log.info("subreddit collected", extra={"subreddit": name, **sub_stats})
            if not sub_stats["window_reached"] and not sub_stats["error"]:
                log.warning("listing limit hit before window start; week is partially covered",
                            extra={"subreddit": name})
        return out
