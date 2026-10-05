"""StockTwits collector (public, unauthenticated v2 stream API).

StockTwits streams are per symbol: GET /api/2/streams/symbol/{SYMBOL}.json returns the newest
~30 messages; `?max=<id>` pages backwards in time. So we must choose WHICH symbols to read:
the pipeline passes a universe (most-discussed / fastest-rising tickers on Reddit from
ApeWisdom + StockTwits trending + STOCKTWITS_WATCHLIST).

What we keep per message: text, author, time, likes (engagement), reply count and the author's
own "Bullish"/"Bearish" tag (StockTwits lets users label their message). A message that appears
in several symbol streams ($NVDA $AMD) is stored once (keyed by message id).

Limits (be honest about them):
  * Unauthenticated rate limit is low (historically ~200 requests/hour per IP). We cap
    requests per run (STOCKTWITS_MAX_REQUESTS_PER_RUN) and pages per symbol
    (STOCKTWITS_MAX_PAGES_PER_SYMBOL, 30 messages each), and wait STOCKTWITS_REQUEST_DELAY
    seconds between calls. Very busy symbols are therefore SAMPLED, not fully read; run the
    collection job several times a day (COLLECT_CRON) so it reads incrementally.
  * Incremental: paging for a symbol stops at the first message we already have.
  * The API may be blocked by StockTwits' bot protection for some networks; failures are
    logged per symbol and never stop the run.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

from ..config import settings
from ..http_client import HttpError, JsonClient
from .base import CollectedWeek, RawPost, week_window_utc

log = logging.getLogger(__name__)
BASE = "https://api.stocktwits.com/api/2"
PLATFORM = "stocktwits"


def parse_time(s: str) -> datetime:
    """'2026-09-29T14:03:11Z' (or with offset) -> naive UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_message(m: dict) -> RawPost | None:
    """One StockTwits message -> RawPost. Returns None for unusable messages."""
    try:
        mid = int(m["id"])
        body = (m.get("body") or "").strip()
        created = parse_time(m["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if not body:
        return None
    user = m.get("user") or {}
    username = user.get("username")
    sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
    likes = (m.get("likes") or {}).get("total") or 0
    replies = (m.get("conversation") or {}).get("replies") or 0
    return RawPost(
        reddit_id=f"st_{mid}",
        subreddit=PLATFORM,
        author=f"st:{username}" if username else None,  # prefixed: no collisions with Reddit usernames
        title="",
        body=body,
        score=int(likes),
        num_comments=int(replies),
        created_utc=created,
        permalink=f"https://stocktwits.com/{username}/message/{mid}" if username else None,
        author_sentiment=sentiment.lower() if isinstance(sentiment, str) and sentiment.lower() in
        ("bullish", "bearish") else None,
    )


class StockTwitsCollector:
    name = "stocktwits"
    incremental = True  # safe to call repeatedly; stops at already-stored messages

    def __init__(self, client: JsonClient | None = None, sleep=time.sleep):
        self.client = client or JsonClient(sleep=sleep)
        self._sleep = sleep
        self.symbols: list[str] = []  # set by the pipeline before collect()
        self.known_ids: set[str] = set()  # reddit_id values already stored ("st_<id>")

    def trending(self) -> list[str]:
        try:
            data = self.client.get_json(f"{BASE}/trending/symbols.json")
            # skip crypto (".X" suffix, e.g. WIF.X) - this tool covers stocks
            return [s["symbol"].upper() for s in data.get("symbols", [])
                    if s.get("symbol") and not s["symbol"].upper().endswith(".X")]
        except Exception as exc:  # noqa: BLE001
            log.warning("stocktwits trending failed", extra={"error": repr(exc)})
            return []

    def collect(self, week_start: date) -> CollectedWeek:
        """Breadth-first: round 1 reads one page for EVERY symbol, later rounds go deeper for the
        symbols that still have older messages to read, until the request budget runs out. So the
        budget is shared fairly instead of being spent on the first few busy symbols."""
        start, end = week_window_utc(week_start)
        out = CollectedWeek(week_start=week_start, platform=PLATFORM, stats={"symbols": {}})
        seen: set[str] = set()
        budget = settings.stocktwits_max_requests
        symbols = list(dict.fromkeys(self.symbols or settings.stocktwits_watchlist))
        state = {sym: {"messages": 0, "pages": 0, "window_reached": False, "error": None, "max_id": None,
                       "done": False} for sym in symbols}

        while budget > 0:
            active = [sym for sym in symbols if not state[sym]["done"]]
            if not active:
                break
            for sym in active:
                if budget <= 0:
                    break
                st = state[sym]
                params = {"max": st["max_id"]} if st["max_id"] else None
                budget -= 1
                st["pages"] += 1
                try:
                    data = self.client.get_json(f"{BASE}/streams/symbol/{sym.replace('.', '-')}.json", params)
                except HttpError as exc:
                    st["error"], st["done"] = f"HTTP {exc.status}", True
                    continue
                except Exception as exc:  # noqa: BLE001
                    st["error"], st["done"] = repr(exc), True
                    continue
                msgs = data.get("messages") or []
                stop = not msgs
                for m in msgs:
                    p = parse_message(m)
                    if p is None:
                        continue
                    if p.reddit_id in self.known_ids:
                        stop = True  # everything older is already stored
                        break
                    if p.created_utc >= end:
                        continue
                    if p.created_utc < start:
                        stop = True
                        break
                    if p.reddit_id not in seen:
                        seen.add(p.reddit_id)
                        out.posts.append(p)
                        st["messages"] += 1
                ids = [int(m["id"]) for m in msgs if "id" in m]
                if stop or not ids:
                    st["window_reached"], st["done"] = True, True
                else:
                    st["max_id"] = min(ids) - 1
                    if st["pages"] >= settings.stocktwits_max_pages:
                        st["done"] = True  # sampled: older messages in the window were not read
                self._sleep(settings.stocktwits_request_delay)

        unread = [sym for sym in symbols if state[sym]["pages"] == 0]
        partial = [sym for sym in symbols if state[sym]["pages"] and not state[sym]["window_reached"]
                   and not state[sym]["error"]]
        for sym in symbols:
            st = {k: v for k, v in state[sym].items() if k not in ("max_id", "done")}
            out.stats["symbols"][sym] = st
            if st["error"]:
                log.warning("stocktwits symbol failed", extra={"symbol": sym, **st})
        out.stats["requests"] = settings.stocktwits_max_requests - budget
        if unread:
            log.warning("stocktwits request budget exhausted before reading some symbols",
                        extra={"unread": unread})
        log.info("stocktwits collected", extra={
            "week_start": week_start.isoformat(), "messages": len(out.posts), "requests": out.stats["requests"],
            "symbols": len(symbols), "sampled_symbols": len(partial)})
        return out
