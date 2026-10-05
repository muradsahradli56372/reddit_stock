# Reddit Stock Intelligence (Reddit Market Pulse)

A **research tool, not trading advice**. It analyses Reddit discussion of public companies and
produces a weekly report: which stocks are discussed most, how many *unique people* discuss them,
sentiment, what is gaining unusual attention versus the previous week, and which subreddits drive it.

**Status: v0.3.** Since Reddit's Data API now requires Reddit's approval, the live setup combines two
key-free sources:

| Source | What it gives | Used for |
|---|---|---|
| **ApeWisdom** (apewisdom.io) | Ticker mention + upvote **counts** on Reddit, per subreddit (no text) | Reddit attention volume, ranking, growth, subreddit split |
| **StockTwits** (public streams) | Message **text**, author, likes, the author's own Bullish/Bearish tag | Sentiment, reasons, unique authors, the full extraction pipeline |

ApeWisdom decides **which** tickers to read on StockTwits (Reddit's most-discussed and fastest-rising
ones, plus StockTwits trending, plus your watchlist). The trend score combines growth on both, and the
dashboard labels which source every number comes from. The official Reddit API collector (PRAW) is
still included for anyone who gets approval. Also included: reasons, Early Signal Score, stock detail
page, price-vs-attention (yfinance), scheduler, structured logs and caching. With nothing configured,
everything runs on realistic demo data.

---

## Quick start

### Option A: Docker (one command)

```bash
cp .env.example .env        # optional, everything works without it
docker compose up --build
```

* Dashboard: http://localhost:5173
* API docs (Swagger): http://localhost:8000/docs

On first start the backend creates the tables and, because the DB is empty, runs the analysis
once (`AUTO_RUN_ON_STARTUP=true`), so the dashboard opens already populated with demo data.

### Option B: no Docker

Requires Python 3.11+ and Node 18+.

```bash
./scripts/dev.sh            # creates .venv, installs deps, starts backend :8000 + dashboard :5173
```

With no `DATABASE_URL` this uses a SQLite file (`data/reddit_stock.db`). To use a local Postgres,
set `DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/reddit_stock` in `.env`.

Manual equivalent:

```bash
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --port 8000          # terminal 1
cd frontend && npm install && npx vite --port 5173      # terminal 2
```

### Run the tests

```bash
cd backend && python -m pytest -q
# against Postgres instead of SQLite:
TEST_DATABASE_URL=postgresql+psycopg://reddit:reddit@localhost:5432/reddit_stock_test python -m pytest -q
```

Tests never touch the network: the Anthropic API, Reddit (PRAW) and yfinance are all tested with fakes.

### Going live (ApeWisdom + StockTwits, no keys needed)

1. Check that both services answer from your machine and that our parsers understand them:
   ```bash
   python scripts/check_sources.py
   ```
2. In `.env`: `TEXT_SOURCE=stocktwits`, `ATTENTION_SOURCE=apewisdom`, `SCHEDULER_ENABLED=true`
   (docker compose enables the scheduler by default).
3. **Keep the backend running.** The collection job (`COLLECT_CRON`, default every 4 hours) takes the
   daily ApeWisdom snapshot and pulls new StockTwits messages. ApeWisdom keeps **no history**, so Reddit
   counts only exist from the day you start collecting. The first week will show
   `days_covered < 7`, and week-over-week Reddit growth appears from the second week.
4. The weekly analysis runs Mondays 06:00 (`SCHEDULE_CRON`, in `REPORT_TIMEZONE`). Or use the
   **Run Analysis** button, or `POST /collect/run` to collect immediately.
5. Optional: `ANTHROPIC_API_KEY` for LLM sentiment/reasons/disambiguation and the summary.

If you do get Reddit Data API approval: set `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` and
`TEXT_SOURCE=reddit` to collect Reddit text directly (`collectors/reddit.py`).

Prices come from yfinance in live mode (`MARKET_DATA_PROVIDER=auto`). In demo mode they are
synthetic, and every chart and label using them says **DEMO**.

---

## Architecture

```
Reddit ─► Collector ─► DB (raw) ─► Dedup ─► Ticker extraction ─► Ambiguous queue ─► Disambiguation
                                                     │                                   │
                                                     └──────────── accepted mentions ◄───┘
                                                                        │
                                       Sentiment (batched) ◄────────────┘
                                                │
                     Weekly metrics + WoW + trend score (pure Python) ─► Weekly report + AI summary
                                                │
                                         FastAPI ─► React dashboard
```

```
backend/app/
  config.py          env vars; demo/LLM switches
  db.py              engine, sessions, portable upsert (Postgres + SQLite)
  models.py          8 tables, unique constraints, indexes
  reference.py       loads data/companies.csv, blacklist.txt, ambiguous.txt
  migrations.py      additive auto-migration (new tables/columns/indexes) on startup
  collectors/        base.py (interface, timezone-aware week math), demo.py, reddit.py (PRAW, live)
  extraction.py      deterministic ticker detection + confidence
  disambiguation.py  ambiguous queue → LLM (batched, cached) or rule-based fallback
  sentiment.py       sentiment + reasons in ONE LLM call per batch (cached) or keyword fallback
  reasons.py         fixed reason taxonomy + keyword fallback
  llm_cache.py       per-item LLM answer cache (table llm_cache)
  metrics.py         weekly aggregation, unique authors, engagement, WoW %   (pure functions)
  trend.py           trend score 0-100 + EMERGING/RISING/STABLE/COOLING   (pure functions)
  signals.py         Early Signal Score 0-100   (pure functions)
  market_data.py     MarketDataProvider, YFinanceProvider, DemoMarketDataProvider, neutral wording
  summary.py         DATA / INTERPRETATION / SPECULATION narrative (template or one LLM call)
  pipeline.py        orchestration, one transaction per week, idempotent
  api.py, api_cache.py, main.py, logging_setup.py   FastAPI app, response cache, scheduler, logs
frontend/src/        React + Recharts: dashboard (App.jsx) and stock detail page (StockPage.jsx, #/stock/TICKER)
data/                reference data (edit these to extend)
```

### How the pipeline works

1. **Collect.** `RedditCollector` (live) or `DemoCollector` (seeded mock data, 5 subreddits,
   `DEMO_HISTORY_WEEKS` weeks of history). Weeks run Monday 00:00 to Sunday 23:59:59 in `REPORT_TIMEZONE`.
   Collection happens before the DB transaction opens, so slow network calls never hold locks.
2. **Store raw.** Authors, subreddits, posts, and comments are upserted on `reddit_id`, so re-collecting is safe.
   If a post or comment's text changed since last time (edited on Reddit), its old mentions are
   dropped and re-extracted. Unchanged content keeps its mentions and already-paid-for sentiment.
3. **Dedup.** The same author posting identical text in the same week counts once (copy-paste spam).
   One post/comment counts as **one mention per ticker** no matter how many times it repeats the ticker.
4. **Extract** (deterministic, `extraction.py`):

   | Method | Example | Confidence |
   |---|---|---|
   | cashtag | `$NVDA` | 0.97 (`$AI` 0.90, explicit cashtag beats blacklist) |
   | alias | `Nvidia`, `Rocket Lab` | 0.85 |
   | ticker | `PLTR` (bare uppercase, 2-5 letters) | 0.80 (0.70 for 2 letters) |
   | ambiguous | `Ford`, `Sofi`/`SOFI`, `Apple`, `Target` | 0.50 → queue |

   Rejected: blacklisted bare words (`AI`, `IT`, `ALL`, `DD`, `YOLO`, `CEO`, `A`, ...) and bare
   single letters (`F`, `T` count only as `$F`, `$T`). Lower-case ambiguous words never match
   (`price target`, `an apple`). Each mention stores ticker, company, method, confidence, the
   matched text, and the surrounding sentence.
5. **Disambiguate** only the ambiguous queue. With a key: batched LLM calls on the fast model.
   Without: rules (person cues like "my buddy Ford" or "Harrison Ford" reject; finance cues like
   "earnings", "shares", "calls" accept; ALL-CAPS `SOFI` with no person cue accepts).
6. **Sentiment + reasons** per mention, in one batched LLM call: bullish / neutral / bearish / unclear
   plus confidence, plus up to 2 reasons from a fixed taxonomy. Only mentions not yet analysed are
   processed, and LLM answers are cached by content (`llm_cache`), so re-runs never pay twice.
7. **Metrics** per stock per week (pure Python): mentions, unique authors, post vs. comment mentions,
   sentiment counts and %, subreddit count and distribution, top-author share, average engagement,
   previous-week values, WoW % for mentions, authors, and comments.
8. **Trend score and class**, plus the **Early Signal Score** (see below).
9. **Prices** for the top `MARKET_DATA_TOP_N` stocks (cached in `weekly_prices`) and a neutral
   attention-vs-price sentence.
10. **Report.** Market overview plus the summary (template, or one LLM call on the stronger model),
    now including top reasons and the price comparison.

### Where AI is used, and where it is not

| Step | Deterministic code | LLM (only with `ANTHROPIC_API_KEY`) |
|---|---|---|
| Ticker detection, blacklist, confidence | ✅ | |
| Ambiguous terms ("Ford", "Sofi") | rule fallback | ✅ batched, fast model |
| Sentiment | keyword fallback (labelled `fallback_keywords`) | ✅ batched, fast model, cached |
| Reasons (why bullish/bearish) | keyword fallback | ✅ same call as sentiment; must pick from the fixed taxonomy |
| Early signal, prices, attention-vs-price wording | ✅ always | never |
| Counting, unique authors, %, WoW, ranking, trend score, DB ops | ✅ always | never |
| Weekly narrative | template from computed numbers | ✅ one call, summary model |

The LLM never computes numbers. The summary model receives the computed metrics as JSON and must
return separate `data` / `interpretation` / `speculation` lists. Malformed output falls back to the
template. Any LLM error (rate limit, network) falls back per batch, so the pipeline never breaks.

### Trend score (0-100)

Defined and documented in `backend/app/trend.py`. It measures growth **relative to the stock's own
baseline** (average of up to 4 previous weeks), not raw volume.

```
g        = log2((current + 5) / (baseline + 5))       clamped to [-3, 3]    # +5 smoothing
s        = (g + 3) / 6                                  # 0..1, 0.5 = flat
core     = 0.6 * s_mentions + 0.4 * s_unique_authors    # one spammer can't fake a trend
breadth  = min(subreddits / 4, 1);  if core > 0.5: core = 0.5 + (core-0.5) * (0.5 + 0.5*breadth)
weight   = min(1, max(current, baseline) / 15)          # minimum-volume floor
score    = 50 + (100*core - 50) * weight
```

* Flat stock = 50, whatever its size (NVDA at 80/week is not "trending").
* Smoothing and the volume floor stop 1 → 4 mentions (+300%) from producing an absurd score (~55).
* Growth confined to one subreddit keeps only about half its upside.

Classes: **EMERGING** score ≥ 75 and ≥ 8 mentions and mentions at least doubled (or new) ·
**RISING** ≥ 60 · **COOLING** ≤ 40 · **STABLE** otherwise.

### Early Signal Score (0-100)

Defined in `backend/app/signals.py`. The trend score asks "is attention unusual for this stock?".
The early signal asks "is a **small** stock starting to get **broad, engaged** attention before it is mainstream?"

```
eligible only if mentions >= 5 and unique authors >= 4        (else 0: noise / one spammer)
G = clamp(log2((m + 3) / (baseline_m + 3)) / 3, 0, 1)          mention growth, 8x -> 1
A = same on unique authors                                     author growth
D = min(subreddits / 3, 1)                                     diversity
E = clamp(log2(1 + avg_score / week_median) / log2(3), 0, 1)   engagement (upvotes per mention)
L = 1 up to 30 mentions, linearly down to 0 at 120             low-volume factor
score = 100 * L * (0.35 G + 0.25 A + 0.20 D + 0.20 E);  flagged when score >= 50 and mentions grew
```

In the demo, ASTS (4 → 26), RKLB (6 → 40) and SOFI (10 → 24) are flagged. NVDA (big, flat) and
PLTR (already large) are not.

### Reasons

`backend/app/reasons.py` defines a fixed taxonomy: 8 bullish categories (earnings growth, product
catalyst, contracts/partnerships, AI/data-center demand, undervalued, momentum, squeeze/meme,
buybacks/balance sheet) and 8 bearish ones (overvaluation, weak results, dilution, competition,
delays, macro, legal/regulatory, technical breakdown). A fixed list means reasons can be **counted**
("12 bullish mentions cite contracts"). A reason must match the mention's stance, and the LLM's
answers are validated against the list. Extend the dictionaries to add categories.

### ApeWisdom: Reddit counts (count-only)

`attention.py`. The collection job fetches `apewisdom.io/api/v1.0/filter/{filter}/page/{n}` for each
of `APEWISDOM_FILTERS` (up to `APEWISDOM_MAX_PAGES` pages) and stores one snapshot per
(community, ticker, day) in `attention_snapshots`; a later snapshot on the same day replaces it.
Tickers not in `companies.csv` are added automatically, using ApeWisdom's name.

```
weekly mentions (per subreddit) = mean(daily 24h counts on covered days) * 7
total = sum over APEWISDOM_FILTERS        (don't combine "all-stocks" with individual subreddits)
```

Every number carries `days_covered`. Reddit week-over-week growth only exists when the previous week
was covered too; otherwise it shows "–", never a made-up %. Limits: no text (so no sentiment, reasons
or unique authors for Reddit), no history before you start collecting, and ApeWisdom's own ticker
detection is a black box to us.

### StockTwits: text

`collectors/stocktwits.py`. Reads `api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json` for each
symbol in the universe, paging back with `?max=` until it passes the start of the week, reaches a
message it already has (incremental), or hits a cap. A message appearing in several streams is stored
once. Authors are stored as `st:<username>`, so they never collide with Reddit usernames. If a message
mentions exactly **one** ticker and its author tagged it Bullish/Bearish, that tag is used as the
sentiment (method `author_label`). Messages with several tickers go to our classifier, because the tag
may refer to only one of them. Limits:

* Unauthenticated rate limit (historically ~200 requests/hour). Caps: `STOCKTWITS_MAX_REQUESTS_PER_RUN`,
  `STOCKTWITS_MAX_PAGES_PER_SYMBOL` (30 messages/page), `STOCKTWITS_REQUEST_DELAY`. Very busy symbols
  (NVDA, TSLA) are **sampled**, so their text counts understate volume. That's why ranking and volume
  use the ApeWisdom counts.
* Only symbols in the universe are read. A ticker that's only on Reddit shows counts but no sentiment
  until you add it to `STOCKTWITS_WATCHLIST`.
* StockTwits' bot protection may block some networks (HTTP 403). `check_sources.py` tells you.
* StockTwits users are a different crowd from Reddit. The dashboard keeps them apart: "Reddit · counts"
  vs. "Text sample".

### How the two are combined

* **Ranking** uses Reddit counts when the week has them, otherwise text mentions.
* **Trend score:** `core = 0.6 * text_core + 0.4 * reddit_growth` (Reddit growth with +20 smoothing,
  since counts are larger). Without Reddit data it's exactly the single-source formula. Breadth counts
  distinct communities (StockTwits + each subreddit).
* **Early signal:** growth = the larger of text and Reddit growth. "Small" is measured on Reddit volume
  **relative to the week's most-discussed ticker** (full credit up to 25% of the leader, zero at 75%),
  so it works at any data scale.
* **Price comparison** uses Reddit growth when available, otherwise text growth, and names its source
  in the sentence.

### Official Reddit API collection and its limits (approval required)

`collectors/reddit.py` pages through `/new` (and `/top?t=month`, to catch popular posts) for each
subreddit until it passes the start of the week. It expands comment trees
(`REDDIT_REPLACE_MORE_LIMIT`) and keeps only items created inside the week. PRAW respects Reddit's
rate-limit headers. Every call also retries with exponential backoff (2s, 4s, 8s, ...) on
429/5xx/network errors. Deleted authors become NULL (excluded from unique-author counts) and
`[deleted]`/`[removed]` bodies are dropped. A private or banned subreddit is logged and skipped
without stopping the run. Honest limits:

* Reddit listings stop at about **1000 items**. A busy subreddit (r/wallstreetbets) can exceed that in a
  week. The run logs `window_reached=False`, and the report's `overview.collection` shows coverage per subreddit.
* There is **no official historical archive**. Collecting a week long after it ended gets whatever is
  still listed. Run weekly (the scheduler) for best coverage.
* Comments made this week on posts created **before** the week aren't collected.
* Scores and engagement are measured at collection time, not at the end of the week.

### Market data

`MarketDataProvider.weekly_changes(tickers, week_start)` has two implementations:
`YFinanceProvider` (previous Friday's close → this week's last close) and `DemoMarketDataProvider`
(deterministic synthetic prices, always labelled `demo`). Add another provider by implementing the
same method. The comparison is always worded as co-occurrence, e.g. *"Reddit attention increased
(+567%) while the share price was roughly flat (+0.6%) over the same week. This is a co-occurrence,
not evidence that one caused the other."* A test enforces that no causal or predictive words appear.

### Database

`authors`, `subreddits`, `posts`, `comments`, `companies`, `stock_mentions` (with sentiment fields),
`weekly_stock_metrics`, `weekly_reports`, plus Phase 2 `mention_reasons`, `weekly_prices`, `llm_cache`.
On startup `migrations.py` creates missing tables and **adds missing columns and indexes**, so a
Phase 1 database upgrades in place (tested, and verified on a real Phase 1 Postgres DB). It handles
additive changes only. Renames or drops would need Alembic.

* `stock_mentions` is unique on `(company_id, source_key)` and indexed on `(ticker, week_start)` →
  "all mentions for PLTR in week X".
* `weekly_stock_metrics` is unique on `(week_start, ticker)` and indexed on `(week_start, mention_change_pct)` →
  "all stocks whose mentions grew > 100%" (`GET /trending?min_growth=100`).
* **Idempotent:** each week is processed in one transaction. Raw data upserts, mentions are
  insert-if-new, metrics and report rows are replaced for that week. A test runs the pipeline twice
  and asserts identical row counts and metrics.

### API

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | demo mode, LLM, market-data provider, timezone, scheduler, cache stats |
| GET | `/weeks` | analysed weeks, newest first |
| GET | `/stocks?week=&limit=&sort=` | ranked metrics; `sort` ∈ mentions, trend_score, unique_authors, mention_change_pct |
| GET | `/stocks/{ticker}?week=` | metrics, reasons, per-subreddit sentiment, top threads, price, recent mentions |
| GET | `/stocks/{ticker}/history?weeks=12` | weekly series: mentions, authors, sentiment, scores, price |
| GET | `/early-signals?week=&include_all=` | flagged early signals (or all scored stocks) |
| GET | `/subreddits?week=` | per-community text activity/sentiment + `reddit_communities` (ApeWisdom counts) |
| POST | `/collect/run` | run the collection job now (ApeWisdom snapshot + new StockTwits messages) |
| GET | `/trending?week=&min_growth=&min_mentions=` | by trend score; `min_growth=100` → grew > 100% |
| GET | `/emerging?week=&include_rising=` | EMERGING (+ RISING) stocks |
| GET | `/sentiment?week=` | overall and per-stock sentiment |
| GET | `/weekly-report?week=` | overview + summary |
| POST | `/analysis/run` | body `{"week_start": "YYYY-MM-DD"}` optional; processes that week and the one before |

GET responses are cached in-process for `API_CACHE_TTL` seconds and cleared after every analysis run.
`week` can be any date and is normalised to its Monday. Errors: 404 for an unknown ticker or a
week that hasn't been analysed, 422 for invalid input, 409 if a run is already in progress.

### Environment variables

See `.env.example`; every variable is documented there. The key ones:
`DATABASE_URL`, `ANTHROPIC_API_KEY`, `LLM_FAST_MODEL`, `LLM_SUMMARY_MODEL`, `REDDIT_CLIENT_ID`,
`REDDIT_CLIENT_SECRET`, `SUBREDDITS`, `REPORT_TIMEZONE`, `MARKET_DATA_PROVIDER`, `SCHEDULER_ENABLED`,
`SCHEDULE_CRON`, `LOG_FORMAT` (`json` for one JSON object per log line), `API_CACHE_TTL`.
API keys are read only by the backend. The frontend calls `/api`, which the Vite server proxies.

### Demo mode

Used automatically when Reddit credentials are missing. It generates `DEMO_HISTORY_WEEKS` weeks (default 8).
The mock data includes:
NVDA, TSLA, PLTR, AAPL, RKLB, ASTS, SOFI (plus AMD, MSFT, AMZN, GME, HOOD); a story
(RKLB and ASTS spike, PLTR and SOFI rise, TSLA cools, NVDA flat); traps ("Ford" and "Sofi" as people,
"Apple pie", AI, IT, ALL, DD, YOLO, CEO, A); deleted authors; and one account
(`diamond_hands_4ever`) writing ~40 NVDA comments, including copy-pastes. The dashboard flags NVDA as
concentrated (one account ≈ 50% of its mentions).

### Extending the reference data

* `data/companies.csv`: `ticker,name,exchange,aliases` (aliases separated by `|`). ~320 common US tickers/ETFs.
  Add rows freely, or generate the file from an exchange listing (e.g. NASDAQ Trader symbol files).
* `data/blacklist.txt`: bare uppercase words never treated as tickers (one per line).
* `data/ambiguous.txt`: `term,TICKER` pairs that need disambiguation. ALL-CAPS terms match
  case-sensitively; others must be written capitalised.

---

## Known limitations (honest list)

* **ApeWisdom, StockTwits, Reddit and yfinance have never been called for real** in the build
  environment (its network policy blocks all four). They are tested with fake HTTP/PRAW/yfinance
  responses built from the documented formats, including an end-to-end live-mode test. Run
  `python scripts/check_sources.py` first; if a format has changed, it shows exactly where.
* Reddit coverage limits: see "Live Reddit collection and its limits" above.
* Keyword reason extraction only finds reasons phrased with known keywords. The LLM path is much better.
* The keyword sentiment fallback is crude: no sarcasm, limited negation, clause-level only. It is
  labelled "fallback" in the DB, the API, and the dashboard. Use an API key for real analysis.
* Rule-based disambiguation is heuristic (e.g. "Ford said it will cut prices" is read as a person).
* Lower-case tickers (`nvda`) are not detected, to avoid false positives.
* One mention per ticker per post/comment. Intensity within a comment is ignored by design.
* The reference list is ~320 tickers, not every listed company.
* Changing the extractor or reference data doesn't retroactively remove mentions from unchanged
  posts (delete that week's `stock_mentions` rows to rebuild). Edited posts *are* handled.
* The API cache is per process. With several workers, use a shared cache (Phase 3).
* `docker compose` was validated with `docker compose config` but not run in the build environment
  (no Docker daemon there). The same stack was verified with a local Postgres 16 instead.

## Roadmap

* **v0.3 (done):** ApeWisdom (Reddit counts) + StockTwits (text) as key-free live sources.
* **Phase 2 (done):** live PRAW collector, reasons, Early Signal Score, per-subreddit sentiment,
  stock detail page, `MarketDataProvider` + yfinance, scheduler, structured logging, caching, history/subreddit endpoints.
* **Phase 3:** auth, alerts, more sources, backtesting, deployment.
