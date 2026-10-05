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
    assert [w["week_start"] for w in r.json()["weeks"]] == [PREV.isoformat(), ANCHOR.isoformat()]


def test_run_analysis_rejects_future(client):
    assert client.post("/analysis/run", json={"week_start": "2999-01-04"}).status_code == 422
