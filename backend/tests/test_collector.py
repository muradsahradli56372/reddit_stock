"""RedditCollector against fake PRAW objects (no network), plus week/timezone math."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.collectors import base
from app.collectors.reddit import RedditCollector, with_retries
from app.config import settings

WEEK = date(2026, 9, 28)


def ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()


class FakeComments:
    def __init__(self, comments):
        self._c = comments
        self.replace_more_calls = 0

    def replace_more(self, limit):
        self.replace_more_calls += 1

    def list(self):
        return self._c


def post(pid, created, title="t", selftext="", author="alice", comments=(), score=10, stickied=False):
    return SimpleNamespace(id=pid, created_utc=created, title=title, selftext=selftext,
                           author=SimpleNamespace(name=author) if author else None, score=score,
                           num_comments=len(comments), permalink=f"/r/x/comments/{pid}/", stickied=stickied,
                           comments=FakeComments(list(comments)))


def comment(cid, created, body="NVDA calls", author="bob"):
    return SimpleNamespace(id=cid, created_utc=created, body=body, score=3,
                           author=SimpleNamespace(name=author) if author else None)


class FakeSubreddit:
    def __init__(self, new, top=()):
        self._new, self._top = new, top

    def new(self, limit=None):
        return iter(self._new)

    def top(self, time_filter="week", limit=None):
        return iter(self._top)


class FakeReddit:
    def __init__(self, subs):
        self.subs = subs

    def subreddit(self, name):
        if name not in self.subs:
            raise PermissionError("private subreddit")
        return self.subs[name]


def test_collects_only_the_window_and_handles_deleted():
    inside = post("a", ts(2026, 9, 29), title="NVDA DD", selftext="[removed]", author=None, comments=[
        comment("c1", ts(2026, 9, 30)),
        comment("c2", ts(2026, 9, 30), body="[deleted]"),
        comment("c3", ts(2026, 10, 6)),  # after the window
        comment("c4", ts(2026, 10, 1), author=None),
    ])
    after = post("b", ts(2026, 10, 6))
    before = post("z", ts(2026, 9, 20))  # stops pagination
    never = post("y", ts(2026, 9, 1))
    reddit = FakeReddit({"stocks": FakeSubreddit([after, inside, before, never], top=[inside])})
    out = RedditCollector(reddit=reddit, subreddits=["stocks"]).collect(WEEK)

    assert [p.reddit_id for p in out.posts] == ["t3_a"]  # deduped across new+top, window applied
    p = out.posts[0]
    assert p.author is None and p.body == "" and p.title == "NVDA DD"
    assert p.permalink == "https://www.reddit.com/r/x/comments/a/"
    assert {c.reddit_id for c in out.comments} == {"t1_c1", "t1_c4"}
    assert [c.author for c in out.comments if c.reddit_id == "t1_c4"] == [None]
    st = out.stats["subreddits"]["stocks"]
    assert st["window_reached"] is True and st["deleted_skipped"] == 1 and not out.is_demo


def test_listing_cap_reports_partial_coverage(monkeypatch):
    monkeypatch.setattr(settings, "reddit_max_posts_per_sub", 3)
    new = [post(f"p{i}", ts(2026, 10, 1)) for i in range(10)]
    out = RedditCollector(reddit=FakeReddit({"wsb": FakeSubreddit(new)}), subreddits=["wsb"]).collect(WEEK)
    assert len(out.posts) == 3
    assert out.stats["subreddits"]["wsb"]["window_reached"] is False


def test_one_failing_subreddit_does_not_stop_the_run():
    ok = FakeSubreddit([post("a", ts(2026, 9, 29))])
    out = RedditCollector(reddit=FakeReddit({"stocks": ok}), subreddits=["private_sub", "stocks"]).collect(WEEK)
    assert len(out.posts) == 1
    assert "private" in out.stats["subreddits"]["private_sub"]["error"]


class TooManyRequests(Exception):
    pass


def test_retries_with_exponential_backoff():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TooManyRequests("429")
        return "ok"
    assert with_retries(flaky, "x", attempts=5, sleep=sleeps.append) == "ok"
    assert sleeps == [2.0, 4.0]


def test_retries_give_up_and_dont_retry_permanent_errors():
    sleeps = []
    with pytest.raises(TooManyRequests):
        with_retries(lambda: (_ for _ in ()).throw(TooManyRequests()), "x", attempts=3, sleep=sleeps.append)
    assert sleeps == [2.0, 4.0]
    sleeps.clear()
    with pytest.raises(ValueError):
        with_retries(lambda: (_ for _ in ()).throw(ValueError("bad")), "x", attempts=3, sleep=sleeps.append)
    assert sleeps == []


def test_collector_retries_transient_listing_errors():
    class Flaky:
        def __init__(self, items):
            self.items, self.failed = list(items), False

        def __iter__(self):
            return self

        def __next__(self):
            if not self.failed:
                self.failed = True
                raise TooManyRequests()
            if not self.items:
                raise StopIteration
            return self.items.pop(0)

    sub = SimpleNamespace(new=lambda limit=None: Flaky([post("a", ts(2026, 9, 29))]), top=lambda **k: iter(()))
    sleeps = []
    out = RedditCollector(reddit=FakeReddit({"s": sub}), subreddits=["s"], sleep=sleeps.append).collect(WEEK)
    assert len(out.posts) == 1 and sleeps == [2.0]


# ---------------------------------------------------------------- week math / timezone
def test_week_math_utc():
    assert base.week_start_of(date(2026, 10, 4)) == WEEK  # Sunday -> Monday before
    assert base.week_start_of(datetime(2026, 9, 28, 0, 0)) == WEEK
    assert base.week_window_utc(WEEK) == (datetime(2026, 9, 28), datetime(2026, 10, 5))
    assert base.last_complete_week(datetime(2026, 10, 5, 9, tzinfo=timezone.utc)) == WEEK
    assert base.last_complete_week(datetime(2026, 10, 4, 23, tzinfo=timezone.utc)) == date(2026, 9, 21)


def test_week_math_respects_report_timezone(monkeypatch):
    monkeypatch.setattr(settings, "report_timezone", "America/New_York")
    # Monday 02:00 UTC is still Sunday evening in New York -> previous week.
    assert base.week_start_of(datetime(2026, 9, 28, 2, 0)) == date(2026, 9, 21)
    start, end = base.week_window_utc(WEEK)
    assert start == datetime(2026, 9, 28, 4, 0)  # EDT = UTC-4
    assert end == datetime(2026, 10, 5, 4, 0)
    # Week containing the November DST change is 7 days + 1 hour long in UTC.
    s, e = base.week_window_utc(date(2026, 10, 26))
    assert (e - s).total_seconds() == 7 * 86400 + 3600
