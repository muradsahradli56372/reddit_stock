"""Realistic, deterministic mock Reddit data for demo mode.

Two consecutive weeks are generated around an anchor week (the last complete week):
  * previous week: "baseline" profile
  * anchor week:   "current" profile (RKLB/ASTS spike, PLTR/SOFI rise, TSLA cools, NVDA flat)
Any other week gets a baseline-like profile, so older weeks also have data.

Built-in traps (must NOT become mentions): "Ford" as a person, "Sofi" as a person, "Apple pie",
"AI", "IT", "ALL", "DD", "YOLO", "CEO", "A".
One heavy author ("diamond_hands_4ever") posts dozens of NVDA comments (some copy-pasted) to
prove unique-author counting and same-author duplicate filtering.

Same week in => byte-identical data out (seeded RNG), so re-collection is idempotent.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from .base import CollectedWeek, RawComment, RawPost, last_complete_week

SUBREDDITS = ["wallstreetbets", "stocks", "investing", "StockMarket", "options"]
HEAVY_AUTHOR = "diamond_hands_4ever"

# ticker -> (surface forms, flavour lines)
FORMS = {
    "NVDA": (["NVDA", "$NVDA", "Nvidia", "NVDA"], ["data center demand", "Blackwell ramp", "hyperscaler capex"]),
    "TSLA": (["TSLA", "$TSLA", "Tesla"], ["delivery numbers", "robotaxi timeline", "margins"]),
    "PLTR": (["PLTR", "Palantir", "$PLTR"], ["government contracts", "AIP bootcamps", "commercial growth"]),
    "AAPL": (["AAPL", "$AAPL", "Apple"], ["iPhone cycle", "services revenue", "buybacks"]),
    "RKLB": (["RKLB", "Rocket Lab", "$RKLB"], ["Neutron first launch", "launch backlog", "space systems revenue"]),
    "ASTS": (["ASTS", "$ASTS", "AST SpaceMobile"], ["BlueBird satellite launch", "carrier partnerships", "spectrum deal"]),
    "SOFI": (["SOFI", "$SOFI", "SoFi"], ["member growth", "loan originations", "bank charter"]),
    "AMD": (["AMD", "$AMD"], ["MI400 chips", "data center share"]),
    "MSFT": (["MSFT", "Microsoft"], ["Azure growth", "Copilot adoption"]),
    "AMZN": (["AMZN", "Amazon"], ["AWS margins", "retail efficiency"]),
    "GME": (["GME", "GameStop"], ["cash pile", "the squeeze crowd"]),
    "HOOD": (["Robinhood", "$HOOD"], ["crypto volumes", "prediction markets"]),
}

# (previous week, anchor week): (mentions, author pool size, sentiment mix bull/bear/neutral, subreddit weights)
PROFILE = {
    "NVDA": ((70, 55, (.55, .20, .25), None), (36, 30, (.55, .20, .25), None)),
    "TSLA": ((62, 48, (.40, .35, .25), None), (34, 28, (.22, .53, .25), None)),
    "PLTR": ((28, 22, (.60, .20, .20), None), (55, 40, (.62, .18, .20), None)),
    "AAPL": ((30, 25, (.35, .25, .40), None), (30, 25, (.33, .27, .40), None)),
    "RKLB": ((6, 5, (.60, .10, .30), [1, 0, 0, 0, 0]), (40, 31, (.70, .10, .20), None)),
    "ASTS": ((4, 4, (.50, .20, .30), [1, 0, 0, 0, 0]), (26, 21, (.65, .15, .20), [3, 2, 1, 1, 2])),
    "SOFI": ((10, 8, (.50, .25, .25), [2, 1, 0, 1, 0]), (24, 19, (.55, .20, .25), None)),
    "AMD": ((15, 13, (.45, .30, .25), None), (14, 12, (.40, .35, .25), None)),
    "MSFT": ((12, 11, (.45, .20, .35), [0, 1, 1, 1, 0]), (11, 10, (.45, .20, .35), [0, 1, 1, 1, 0])),
    "AMZN": ((10, 9, (.45, .20, .35), None), (12, 11, (.45, .20, .35), None)),
    "GME": ((8, 6, (.50, .30, .20), [1, 0, 0, 0, 0]), (5, 5, (.40, .40, .20), [1, 0, 0, 0, 0])),
    "HOOD": ((6, 6, (.50, .25, .25), None), (9, 8, (.55, .20, .25), None)),
}

BULLISH = [
    "{T} is going to explode after earnings 🚀",
    "Loaded up on {T} calls this morning, this thing is still undervalued",
    "I sold {T} at ${P} and regret it so much",
    "{T} breakout looks real, adding more on any dip",
    "Long {T}. The {F} story is just getting started",
    "Can't believe how cheap {T} still is. Buying more.",
    "{T} {F} keeps getting stronger, bullish into next year",
    "Should have bought more {T} last month, the {F} news is huge",
]
BEARISH = [
    "{T} is ridiculously overvalued at these levels",
    "Bought puts on {T}, this rally makes no sense",
    "{T} looks like a bubble to me, staying away",
    "Glad I sold {T} before the drop",
    "{T} guidance on {F} was weak, expecting it to tank",
    "Too much dilution at {T}, bagholders everywhere",
    "The {F} hype around {T} is priced in, I'm shorting it",
]
NEUTRAL = [
    "What's everyone's take on {T} ahead of earnings?",
    "Anyone have good DD on {T} and the {F} situation?",
    "{T} reports on Thursday after the close, what are you watching?",
    "How much of your portfolio is in {T} right now?",
    "Comparing {T} with the S&P over 5 years, interesting chart",
    "Any thoughts on how {F} affects {T}?",
]
POST_TITLES = {
    "bullish": ["{T} is my highest conviction play", "Why I'm adding {T} this week", "{T} to the moon? My DD"],
    "bearish": ["{T} is overvalued, change my mind", "Sold all my {T}, here's why", "Bear case for {T}"],
    "neutral": ["{T} discussion thread", "Thoughts on {T}?", "{T} earnings preview"],
}
HEAVY_LINES = [
    "NVDA to $500 by Christmas, mark my words 🚀",
    "Still holding NVDA, diamond hands",
    "NVDA dip is a gift, bought more",
    "Nobody is selling NVDA, the data center demand is insane and bullish",
    "NVDA calls printing again",
]
# Sentences that look like tickers but are not (each must yield zero mentions).
TRAPS = [
    "My buddy Ford says I should diversify, not sure he's right.",
    "Harrison Ford was great in that movie, anyway back to the charts.",
    "My coworker Sofi thinks the whole market is rigged lol",
    "AI is going to change everything but IT spending is ALL that matters for now.",
    "Great DD, YOLO'd my bonus into index funds instead.",
    "The CEO said A lot of things on the call but nothing concrete.",
    "Made an Apple pie this weekend instead of watching the market.",
    "Is it just me or is this sub all FOMO and no DD these days?",
]
NOISE = [
    "Market is wild today", "Anyone else just holding index funds?", "Fed meeting next week, buckle up",
    "This sub never disappoints", "Down 20% this month, send help", "What's a good book on options?",
]


def _seed(week_start: date) -> int:
    return int(week_start.strftime("%Y%m%d"))


def _rand_time(rng: random.Random, week_start: date) -> datetime:
    return datetime.combine(week_start, time()) + timedelta(seconds=rng.randint(0, 7 * 86400 - 1))


class DemoCollector:
    name = "demo"

    def __init__(self, anchor_week: date | None = None):
        self.anchor = anchor_week or last_complete_week()

    def _profile_index(self, week_start: date) -> int:
        return 1 if week_start == self.anchor else 0

    def collect(self, week_start: date) -> CollectedWeek:
        rng = random.Random(_seed(week_start))
        idx = self._profile_index(week_start)
        wk = week_start.strftime("%Y%m%d")
        out = CollectedWeek(week_start=week_start, is_demo=True)
        # Same community every week (fixed seed), so many authors are active in both weeks.
        community = random.Random(42)
        authors = [f"user_{community.randrange(10**6):06d}" for _ in range(260)]
        counter = {"p": 0, "c": 0}

        def new_id(kind: str) -> str:
            counter[kind] += 1
            return f"demo{wk}{kind}{counter[kind]:05d}"

        # Daily discussion threads per subreddit: comments attach here.
        threads: dict[str, list[str]] = {s: [] for s in SUBREDDITS}
        for sub in SUBREDDITS:
            for day in range(7):
                pid = new_id("p")
                d = week_start + timedelta(days=day)
                out.posts.append(RawPost(pid, sub, "AutoModerator", f"Daily Discussion Thread for {d:%B %d, %Y}",
                                         "Use this thread for general discussion.", rng.randint(20, 400), 0,
                                         datetime.combine(d, time(6, 0))))
                threads[sub].append(pid)

        def fill(template: str, ticker: str) -> str:
            forms, flavours = FORMS[ticker]
            return template.format(T=rng.choice(forms), F=rng.choice(flavours), P=rng.choice([45, 80, 100, 120, 150]))

        for ticker, profiles in PROFILE.items():
            n, pool_size, (pb, pr, _pn), weights = profiles[idx]
            # Each ticker has its own author pool so unique-author counts follow the profile.
            pool = rng.sample(authors, min(pool_size, len(authors)))
            for i in range(n):
                r = rng.random()
                label = "bullish" if r < pb else "bearish" if r < pb + pr else "neutral"
                sub = rng.choices(SUBREDDITS, weights=weights)[0] if weights else rng.choice(SUBREDDITS)
                author = pool[i % len(pool)] if i < len(pool) else rng.choice(pool)
                if rng.random() < 0.03:
                    author = None  # deleted account
                ts = _rand_time(rng, week_start)
                tmpl = {"bullish": BULLISH, "bearish": BEARISH, "neutral": NEUTRAL}[label]
                if rng.random() < 0.22:  # a post
                    title = fill(rng.choice(POST_TITLES[label]), ticker)
                    body = fill(rng.choice(tmpl), ticker)
                    pid = new_id("p")
                    out.posts.append(RawPost(pid, sub, author, title, body, rng.randint(1, 3000),
                                             rng.randint(0, 200), ts))
                    threads[sub].append(pid)
                else:
                    out.comments.append(RawComment(new_id("c"), rng.choice(threads[sub]), author,
                                                   fill(rng.choice(tmpl), ticker), rng.randint(-5, 500), ts))

        # Heavy author: many NVDA comments, several of them identical copy-pastes.
        heavy_n = 40 if idx == 1 else 3
        for i in range(heavy_n):
            line = HEAVY_LINES[0] if i % 8 == 0 else rng.choice(HEAVY_LINES[1:])
            out.comments.append(RawComment(new_id("c"), rng.choice(threads["wallstreetbets"]), HEAVY_AUTHOR,
                                           line if i % 8 == 0 else f"{line} (#{i})", rng.randint(0, 50),
                                           _rand_time(rng, week_start)))

        # Traps and noise: lots of activity that must not produce mentions.
        for _ in range(60):
            sub = rng.choice(SUBREDDITS)
            out.comments.append(RawComment(new_id("c"), rng.choice(threads[sub]), rng.choice(authors),
                                           rng.choice(TRAPS + NOISE), rng.randint(0, 80), _rand_time(rng, week_start)))

        out.posts.sort(key=lambda p: p.created_utc)
        out.comments.sort(key=lambda c: c.created_utc)
        return out
