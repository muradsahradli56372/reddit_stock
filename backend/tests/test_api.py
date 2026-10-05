import pytest
from fastapi.testclient import TestClient

from app.main import app

from .conftest import ANCHOR, PREV


@pytest.fixture(scope="module")
def client(analysed_db):
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["llm_enabled"] is False


def test_weeks(client):
    weeks = [w["week_start"] for w in client.get("/weeks").json()]
    assert weeks[:2] == [ANCHOR.isoformat(), PREV.isoformat()]


def test_stocks_default_latest_week(client):
    body = client.get("/stocks").json()
    assert body["week_start"] == ANCHOR.isoformat()
    ranks = [s["rank"] for s in body["stocks"]]
    assert ranks == sorted(ranks)
    first = body["stocks"][0]
    for key in ("ticker", "company", "mentions", "mention_change_pct", "bullish_pct", "bearish_pct", "trend_score"):
        assert key in first


def test_stocks_any_day_normalises_to_week(client):
    assert client.get("/stocks", params={"week": "2026-10-01"}).json()["week_start"] == ANCHOR.isoformat()


def test_stocks_sort_validation(client):
    assert client.get("/stocks", params={"sort": "drop table"}).status_code == 422


def test_stock_detail(client):
    body = client.get("/stocks/rklb").json()
    assert body["ticker"] == "RKLB" and body["company"].startswith("Rocket Lab")
    assert body["metrics"]["trend_class"] == "EMERGING"
    assert body["recent_mentions"] and body["recent_mentions"][0]["sentiment"]


def test_stock_detail_unknown_ticker(client):
    r = client.get("/stocks/ZZZZZ")
    assert r.status_code == 404 and "Unknown ticker" in r.json()["detail"]


def test_unknown_week_404_and_bad_date_422(client):
    assert client.get("/stocks", params={"week": "2020-01-06"}).status_code == 404
    assert client.get("/stocks", params={"week": "not-a-date"}).status_code == 422


def test_trending_min_growth_filter(client):
    body = client.get("/trending", params={"min_growth": 100}).json()
    assert body["stocks"]
    assert all(s["mention_change_pct"] is not None and s["mention_change_pct"] > 100 for s in body["stocks"])
    scores = [s["trend_score"] for s in body["stocks"]]
    assert scores == sorted(scores, reverse=True)


def test_emerging(client):
    stocks = client.get("/emerging").json()["stocks"]
    assert {"RKLB", "ASTS"} <= {s["ticker"] for s in stocks}
    assert all(s["trend_class"] in ("EMERGING", "RISING") for s in stocks)
    only = client.get("/emerging", params={"include_rising": False}).json()["stocks"]
    assert all(s["trend_class"] == "EMERGING" for s in only)


def test_sentiment(client):
    body = client.get("/sentiment").json()
    overall = body["overall"]
    assert overall["total"] > 0
    assert abs(sum(overall[k]["pct"] for k in ("bullish", "neutral", "bearish", "unclear")) - 100) < 0.5
    assert body["methods"] == ["fallback_keywords"]


def test_weekly_report(client):
    body = client.get("/weekly-report", params={"week": PREV.isoformat()}).json()
    assert body["week_start"] == PREV.isoformat()
    assert set(body["summary"]) >= {"data", "interpretation", "speculation"}
    assert body["overview"]["subreddit_activity"]


def test_run_analysis_endpoint(client):
    r = client.post("/analysis/run", json={"week_start": ANCHOR.isoformat()})
    assert r.status_code == 200
    weeks = [w["week_start"] for w in r.json()["weeks"]]
    assert weeks[-2:] == [PREV.isoformat(), ANCHOR.isoformat()] and weeks == sorted(weeks)


def test_run_analysis_rejects_future(client):
    assert client.post("/analysis/run", json={"week_start": "2999-01-04"}).status_code == 422


# ---------------------------------------------------------------- Phase 2 endpoints
def test_stock_detail_phase2_fields(client):
    body = client.get("/stocks/RKLB").json()
    r = body["reasons"]
    assert r["bullish"] and r["bullish"][0]["count"] >= r["bullish"][-1]["count"]
    assert {"category", "label", "count"} <= set(r["bullish"][0])
    assert r["methods"] == ["fallback_keywords"]
    subs = body["subreddit_sentiment"]
    assert sum(s["mentions"] for s in subs) == body["metrics"]["mentions"]
    assert all(-100 <= s["net_sentiment"] <= 100 for s in subs)
    tp = body["top_posts"]
    assert tp and all("RKLB" in p["title"] or "Rocket Lab" in p["title"] for p in tp[:3])
    assert body["price"]["source"] == "demo"
    assert "not evidence that one caused the other" in body["price"]["attention_vs_price"]


def test_history(client):
    body = client.get("/stocks/rklb/history", params={"weeks": 8}).json()
    weeks = body["weeks"]
    assert len(weeks) == 8 and [w["week_start"] for w in weeks] == sorted(w["week_start"] for w in weeks)
    assert weeks[-1]["week_start"] == ANCHOR.isoformat() and weeks[-1]["trend_class"] == "EMERGING"
    assert weeks[-1]["mentions"] > 3 * weeks[-2]["mentions"]
    assert client.get("/stocks/ZZZZZ/history").status_code == 404
    assert client.get("/stocks/RKLB/history", params={"weeks": 0}).status_code == 422


def test_history_zero_fills_weeks_without_mentions(client):
    weeks = client.get("/stocks/KO/history").json()["weeks"]  # Coca-Cola: never mentioned in demo
    assert weeks and all(w["mentions"] == 0 and w["trend_score"] is None for w in weeks)


def test_subreddits(client):
    subs = client.get("/subreddits").json()["subreddits"]
    assert {s["subreddit"] for s in subs} == {"wallstreetbets", "stocks", "investing", "StockMarket", "options"}
    wsb = next(s for s in subs if s["subreddit"] == "wallstreetbets")
    assert wsb["top_tickers"][0]["ticker"] == "NVDA"  # the heavy poster lives in WSB
    assert all(s["mentioning_authors"] <= s["mentions"] for s in subs)


def test_early_signals(client):
    flagged = client.get("/early-signals").json()["stocks"]
    tickers = [s["ticker"] for s in flagged]
    assert {"RKLB", "ASTS"} <= set(tickers) and "NVDA" not in tickers
    assert all(s["is_early_signal"] for s in flagged)
    scores = [s["early_signal_score"] for s in flagged]
    assert scores == sorted(scores, reverse=True)
    everything = client.get("/early-signals", params={"include_all": True}).json()["stocks"]
    assert len(everything) >= len(flagged)


def test_response_cache_hits_and_clears(client):
    from app import api_cache
    api_cache.clear()
    before = dict(api_cache.stats)
    client.get("/stocks/PLTR")
    client.get("/stocks/PLTR")
    assert api_cache.stats["hits"] == before["hits"] + 1
    client.post("/analysis/run", json={"week_start": ANCHOR.isoformat()})
    client.get("/stocks/PLTR")
    assert api_cache.stats["misses"] == before["misses"] + 2  # cache was cleared by the run


# ---------------------------------------------------------------- v0.3: ApeWisdom + StockTwits
def test_health_reports_sources(client):
    h = client.get("/health").json()
    assert h["text_source"] == "demo" and h["attention_source"] == "demo"
    assert h["scheduler"]["collect_cron"]


def test_reddit_attention_in_stocks_and_overview(client):
    stocks = client.get("/stocks").json()["stocks"]
    ranks = [s["rank"] for s in stocks]
    assert ranks == sorted(ranks)  # default order = attention rank
    by = {s["ticker"]: s for s in stocks}
    assert by["OKLO"]["mentions"] == 0 and by["OKLO"]["reddit_mentions"] > 100  # Reddit-only ticker
    assert by["RKLB"]["reddit_change_pct"] > 100 and by["RKLB"]["reddit_days_covered"] == 7
    ov = client.get("/weekly-report").json()["overview"]["reddit_attention"]
    assert ov["source"] == "demo" and ov["days_covered"] == 7 and ov["total_mentions"] > 0


def test_reddit_communities(client):
    comms = client.get("/subreddits").json()["reddit_communities"]
    assert comms and comms[0]["community"] == "wallstreetbets"
    assert all(c["top_tickers"] for c in comms)


def test_history_has_reddit_series(client):
    weeks = client.get("/stocks/OKLO/history").json()["weeks"]
    assert weeks[-1]["reddit_mentions"] > 3 * weeks[-2]["reddit_mentions"]


def test_collect_endpoint(client, monkeypatch):
    from app import pipeline
    monkeypatch.setattr(pipeline, "collect_now", lambda: {"attention": {"rows": 0}})
    assert client.post("/collect/run").json() == {"attention": {"rows": 0}}
