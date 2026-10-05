# Reddit Stock Intelligence (Reddit Market Pulse)

A **research tool, not trading advice**. It analyses Reddit discussion of public companies and
produces a weekly report: which stocks are discussed most, how many *unique people* discuss them,
sentiment, what is gaining unusual attention versus the previous week, and which subreddits drive it.

**Status: Phase 1 (demo-first vertical slice).** Mock Reddit data with real extraction, sentiment,
metrics, trend scoring, API and dashboard. The live Reddit collector is Phase 2.

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

Tests never call the Anthropic API. The LLM code paths are tested with a fake client.

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
  collectors/        base.py (interface, week math), demo.py (mock data). Phase 2: reddit.py
  extraction.py      deterministic ticker detection + confidence
  disambiguation.py  ambiguous queue → LLM (batched) or rule-based fallback
  sentiment.py       finance-aware sentiment → LLM (batched) or keyword fallback
  metrics.py         weekly aggregation, unique authors, WoW %   (pure functions)
  trend.py           trend score 0-100 + EMERGING/RISING/STABLE/COOLING   (pure functions)
  summary.py         DATA / INTERPRETATION / SPECULATION narrative (template or one LLM call)
  pipeline.py        orchestration, one transaction per week, idempotent
  api.py, main.py    FastAPI app, optional weekly scheduler
frontend/src/        React + Recharts dashboard ("Reddit Market Pulse")
data/                reference data (edit these to extend)
```

### How the pipeline works

1. **Collect.** Phase 1 uses `DemoCollector`: seeded, realistic posts/comments across 5 subreddits for
   two consecutive weeks (last complete ISO week plus the one before). Weeks are Monday 00:00 to Sunday 23:59 UTC.
2. **Store raw.** Authors, subreddits, posts, and comments are upserted on `reddit_id`, so re-collecting is safe.
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
6. **Sentiment** per mention: bullish / neutral / bearish / unclear plus confidence, only for mentions
   that don't have one yet (re-runs don't re-pay for LLM calls).
7. **Metrics** per stock per week (pure Python): mentions, unique authors, post vs. comment mentions,
   sentiment counts and %, subreddit count and distribution, top-author share, previous-week values,
   WoW % for mentions, authors, and comments.
8. **Trend score and class** (see below).
9. **Report.** Market overview plus the summary (template, or one LLM call on the stronger model).

### Where AI is used, and where it is not

| Step | Deterministic code | LLM (only with `ANTHROPIC_API_KEY`) |
|---|---|---|
| Ticker detection, blacklist, confidence | ✅ | |
| Ambiguous terms ("Ford", "Sofi") | rule fallback | ✅ batched, fast model |
| Sentiment | keyword fallback (labelled `fallback_keywords`) | ✅ batched, fast model |
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

### Database

`authors`, `subreddits`, `posts`, `comments`, `companies`, `stock_mentions` (with sentiment fields),
`weekly_stock_metrics`, `weekly_reports`. Tables are created with `create_all` on startup (Alembic
can be added once the schema stabilises).

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
| GET | `/health` | demo mode / LLM status |
| GET | `/weeks` | analysed weeks, newest first |
| GET | `/stocks?week=&limit=&sort=` | ranked metrics; `sort` ∈ mentions, trend_score, unique_authors, mention_change_pct |
| GET | `/stocks/{ticker}?week=` | metrics + recent mentions with context, method, and sentiment |
| GET | `/trending?week=&min_growth=&min_mentions=` | by trend score; `min_growth=100` → grew > 100% |
| GET | `/emerging?week=&include_rising=` | EMERGING (+ RISING) stocks |
| GET | `/sentiment?week=` | overall and per-stock sentiment |
| GET | `/weekly-report?week=` | overview + summary |
| POST | `/analysis/run` | body `{"week_start": "YYYY-MM-DD"}` optional; processes that week and the one before |

`week` can be any date and is normalised to its Monday. Errors: 404 for an unknown ticker or a
week that hasn't been analysed, 422 for invalid input, 409 if a run is already in progress.

### Environment variables

See `.env.example`; every variable is documented there. The key ones:
`DATABASE_URL`, `ANTHROPIC_API_KEY`, `LLM_FAST_MODEL`, `LLM_SUMMARY_MODEL`, `REDDIT_CLIENT_ID`,
`REDDIT_CLIENT_SECRET`, `AUTO_RUN_ON_STARTUP`, `SCHEDULER_ENABLED`.
API keys are read only by the backend. The frontend calls `/api`, which the Vite server proxies.

### Demo mode

Phase 1 always uses demo data (the live collector is Phase 2). The mock data includes:
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

* **No live Reddit data yet.** Setting Reddit credentials does nothing in Phase 1.
* The keyword sentiment fallback is crude: no sarcasm, limited negation, clause-level only. It is
  labelled "fallback" in the DB, the API, and the dashboard. Use an API key for real analysis.
* Rule-based disambiguation is heuristic (e.g. "Ford said it will cut prices" is read as a person).
* Lower-case tickers (`nvda`) are not detected, to avoid false positives.
* One mention per ticker per post/comment. Intensity within a comment is ignored by design.
* The reference list is ~320 tickers, not every listed company.
* Re-running a week doesn't delete mentions that a *changed* extractor would no longer find
  (rebuild by deleting that week's `stock_mentions` rows). Ambiguous items rejected earlier are
  re-checked on re-run (free with rules, costs LLM calls with a key; Phase 2 adds caching).
* `docker compose` was validated with `docker compose config` but not run in the build environment
  (no Docker daemon there). The same stack was verified with a local Postgres 16 instead.

## Roadmap

* **Phase 2:** live PRAW collector (pagination, rate limits, backoff, deleted content, previous 7
  complete days); reason extraction; Early Signal Score; per-subreddit sentiment; stock detail page;
  `MarketDataProvider` (yfinance) with neutral price-vs-attention wording; scheduler and logging;
  `/stocks/{ticker}/history`, `/subreddits`.
* **Phase 3:** auth, alerts, more sources, backtesting, deployment.
