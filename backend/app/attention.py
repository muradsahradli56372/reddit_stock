"""Count-only Reddit attention (no text): providers + weekly aggregation.

ApeWisdom (https://apewisdom.io) counts ticker mentions on Reddit and publishes rolling
24-hour numbers per filter (mostly subreddit names), free and without an API key:
    GET https://apewisdom.io/api/v1.0/filter/{filter}/page/{n}
    -> {"count", "pages", "currentPage", "results": [{"rank", "ticker", "name", "mentions",
        "upvotes", "rank_24h_ago", "mentions_24h_ago"}]}
It keeps no history, so the collection job stores one snapshot per day and we estimate:

    weekly mentions (per community) = mean(daily 24h counts on covered days) * 7

`days_covered` is reported with every number so partial weeks are visible, never hidden.
The total across communities is the sum over APEWISDOM_FILTERS (configure filters that don't
overlap, e.g. individual subreddits, not "all-stocks" together with them).
"""
from __future__ import annotations

import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from .config import settings
from .http_client import HttpError, JsonClient

log = logging.getLogger(__name__)
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/{page}"


@dataclass
class SnapshotRow:
    community: str
    ticker: str
    mentions: int
    upvotes: int
    rank: int | None = None
    name: str | None = None


class AttentionProvider(Protocol):
    name: str

    def snapshot(self, day: date) -> list[SnapshotRow]: ...


def _int(v) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def parse_apewisdom_page(data: dict, community: str) -> list[SnapshotRow]:
    rows = []
    for r in data.get("results") or []:
        tk = str(r.get("ticker") or "").strip().upper()
        if not tk or len(tk) > 10:
            continue
        rows.append(SnapshotRow(community, tk, _int(r.get("mentions")), _int(r.get("upvotes")),
                                _int(r.get("rank")) or None, (r.get("name") or None)))
    return rows


class ApeWisdomProvider:
    name = "apewisdom"
    live_only = True  # can only snapshot "now" (no history)

    def __init__(self, client: JsonClient | None = None):
        self.client = client or JsonClient()

    def snapshot(self, day: date) -> list[SnapshotRow]:
        rows: list[SnapshotRow] = []
        for flt in settings.apewisdom_filters:
            page, pages = 1, 1
            while page <= min(pages, settings.apewisdom_max_pages):
                try:
                    data = self.client.get_json(APEWISDOM_URL.format(filter=flt, page=page))
                except HttpError as exc:
                    log.warning("apewisdom filter failed", extra={"filter": flt, "status": exc.status})
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("apewisdom filter failed", extra={"filter": flt, "error": repr(exc)})
                    break
                rows.extend(parse_apewisdom_page(data, flt))
                pages = _int(data.get("pages")) or 1
                page += 1
        log.info("apewisdom snapshot", extra={"rows": len(rows), "filters": settings.apewisdom_filters})
        return rows


class DemoAttentionProvider:
    """Synthetic ApeWisdom-like daily counts that follow the demo story (labelled source=demo)."""
    name = "demo"
    live_only = False
    COMMUNITIES = ["wallstreetbets", "stocks", "investing", "options", "Daytrading"]
    # Reddit counts are larger than a text sample, and mega-caps dominate Reddit more.
    BIG = {"NVDA": 30, "TSLA": 30, "PLTR": 30, "AAPL": 20, "AMD": 15, "MSFT": 15, "AMZN": 15}
    SCALE = 10
    # Reddit-only tickers (no demo text): weekly counts (previous weeks, anchor week).
    # OKLO surges on Reddit in the anchor week although our text sample never sees it.
    EXTRA = {"OKLO": (60, 330), "HIMS": (300, 280), "LUNR": (120, 140), "MSTR": (450, 420)}

    def __init__(self, anchor_week: date | None = None):
        from .collectors.demo import DemoCollector
        self.demo = DemoCollector(anchor_week)

    def snapshot(self, day: date) -> list[SnapshotRow]:
        from .collectors.demo import PROFILE
        week = day - timedelta(days=day.weekday())
        idx = self.demo._profile_index(week)
        rng = random.Random(f"att:{day}")
        rows = []
        weekly = {tk: prof[idx][0] * self.demo._scale(week, tk, self.demo.anchor) * self.BIG.get(tk, self.SCALE)
                  for tk, prof in PROFILE.items()}
        weekly["NVDA"] += (36 if idx == 1 else 3) * self.BIG["NVDA"]  # the heavy poster's comments count too
        weekly.update({tk: v[idx] for tk, v in self.EXTRA.items()})
        for tk, n in weekly.items():
            wsb_heavy = tk in ("NVDA", "GME", "TSLA", "ASTS", "RKLB", "MSTR")
            weights = [5, 2, 1, 1, 1] if wsb_heavy else [2, 3, 2, 1, 1]
            daily_total = n / 7 * rng.uniform(0.75, 1.25)
            for c, w in zip(self.COMMUNITIES, weights):
                m = round(daily_total * w / sum(weights))
                if m > 0:
                    rows.append(SnapshotRow(c, tk, m, round(m * rng.uniform(3, 9))))
        return rows


def get_attention_provider() -> AttentionProvider | None:
    src = settings.resolved_attention_source
    if src == "none":
        return None
    if src == "demo":
        return DemoAttentionProvider()
    return ApeWisdomProvider()


# ---------------------------------------------------------------- weekly aggregation (pure)
@dataclass
class RedditWeek:
    ticker: str
    mentions: float = 0.0  # estimated weekly mentions (sum over communities)
    upvotes: float = 0.0
    distribution: dict = field(default_factory=dict)  # community -> estimated weekly mentions
    days_covered: int = 0


def aggregate_week(rows: list[tuple[str, str, date, int, int]]) -> tuple[dict[str, RedditWeek], int]:
    """rows: (community, ticker, snapshot_date, mentions, upvotes) for one week.

    Returns ({ticker: RedditWeek}, days_covered). A community's covered days are the distinct
    dates it was snapshotted; a ticker absent from a covered day's snapshot counts as 0 that day.
    """
    days_by_comm: dict[str, set] = defaultdict(set)
    sums: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for comm, tk, d, m, u in rows:
        days_by_comm[comm].add(d)
        s = sums[(tk, comm)]
        s[0] += m
        s[1] += u
    all_days = set().union(*days_by_comm.values()) if days_by_comm else set()
    out: dict[str, RedditWeek] = {}
    for (tk, comm), (m, u) in sums.items():
        n_days = len(days_by_comm[comm])
        est_m, est_u = m / n_days * 7, u / n_days * 7
        w = out.setdefault(tk, RedditWeek(tk, days_covered=len(all_days)))
        w.mentions += est_m
        w.upvotes += est_u
        w.distribution[comm] = round(est_m, 1)
    for w in out.values():
        w.mentions, w.upvotes = round(w.mentions, 1), round(w.upvotes, 1)
        w.distribution = dict(sorted(w.distribution.items(), key=lambda x: -x[1]))
    return out, len(all_days)
