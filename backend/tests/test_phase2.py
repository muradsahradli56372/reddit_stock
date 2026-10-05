"""Reasons, early signal score, market data, migrations, LLM cache."""
import json
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text

from app import llm, market_data, reasons, sentiment, signals
from app.config import settings
from app.db import session_scope
from app.llm_cache import Cache
from app.migrations import migrate


# ---------------------------------------------------------------- reasons
@pytest.mark.parametrize("text_,stance,expected", [
    ("Long PLTR, the government contracts keep coming", "bullish", "contracts_partnerships"),
    ("NVDA data center demand is insane", "bullish", "ai_datacenter_demand"),
    ("Too much dilution at ASTS", "bearish", "dilution"),
    ("RKLB is overvalued, bubble territory", "bearish", "overvaluation"),
    ("Neutron delays again, pushed back to next year", "bearish", "execution_delays"),
])
def test_fallback_reasons(text_, stance, expected):
    assert (stance, expected) in reasons.fallback_reasons(text_, stance)


def test_reasons_follow_stance_only():
    assert reasons.fallback_reasons("overvalued but the contracts are great", "bullish") == \
        [("bullish", "contracts_partnerships")]
    assert reasons.fallback_reasons("great contracts", "neutral") == []


def test_sentiment_fallback_includes_reasons():
    r = sentiment.fallback_sentiment("Too much dilution at SOFI, bagholders everywhere", "SOFI")
    assert r.label == "bearish" and ("bearish", "dilution") in r.reasons


# ---------------------------------------------------------------- LLM reasons + cache
@pytest.fixture
def fake_llm(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_enabled", True)
    calls = []

    def fake(model, system, user, max_tokens=2048):
        items = json.loads(user)
        calls.append(len(items))
        return [{"id": i["id"], "sentiment": "bullish", "confidence": 0.9,
                 "reasons": ["earnings_growth", "not_a_category", "overvaluation"]} for i in items]
    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def test_llm_reasons_validated_against_taxonomy(fake_llm):
    [r] = sentiment.classify([("NVDA", "NVDA beat earnings", "NVDA")])
    # unknown category dropped; bearish category on a bullish stance dropped
    assert r.method == "llm" and r.reasons == [("bullish", "earnings_growth")]


def test_llm_cache_avoids_second_call(fake_llm, analysed_db):
    items = [("PLTR", f"PLTR unique cache test {i}", "PLTR") for i in range(3)]
    with session_scope() as s:
        first = sentiment.classify(items, Cache(s))
    with session_scope() as s:
        second = sentiment.classify(items, Cache(s))
    assert fake_llm == [3]  # second run fully served from llm_cache
    assert [r.label for r in first] == [r.label for r in second] and second[0].method == "llm"


# ---------------------------------------------------------------- early signal
def test_early_signal_eligibility_floor():
    assert signals.early_signal_score(4, 0, 4, 0, 3, 100, 50) == 0.0  # too few mentions
    assert signals.early_signal_score(20, 2, 3, 1, 3, 100, 50) == 0.0  # too few people (spam)


def test_early_signal_prefers_small_broad_engaged_growth():
    small = signals.early_signal_score(25, 4, 20, 4, 4, 200, 100)
    big = signals.early_signal_score(250, 40, 200, 40, 4, 200, 100)  # same growth, mainstream volume
    narrow = signals.early_signal_score(25, 4, 20, 4, 1, 200, 100)
    unengaged = signals.early_signal_score(25, 4, 20, 4, 4, 0, 100)
    flat = signals.early_signal_score(25, 25, 20, 20, 4, 200, 100)
    assert small >= 50 and signals.is_early_signal(small, 25, 4)
    assert big == 0.0
    assert small > narrow and small > unengaged and small > flat
    assert not signals.is_early_signal(flat, 25, 25)


def test_low_volume_factor():
    assert signals.low_volume_factor(10) == 1.0
    assert signals.low_volume_factor(75) == pytest.approx(0.5)
    assert signals.low_volume_factor(500) == 0.0


def test_early_signal_bounds():
    for args in [(5, 0, 4, 0, 10, 1e9, 1), (120, 0, 120, 0, 10, 0, 0), (5, 1000, 4, 1000, 1, 0, 0)]:
        assert 0 <= signals.early_signal_score(*args) <= 100


# ---------------------------------------------------------------- market data
@pytest.mark.parametrize("att,price,must", [
    (567.0, 4.2, "increased (+567%) while the share price also rose (+4.2%)"),
    (567.0, -3.0, "increased (+567%) while the share price fell (-3.0%)"),
    (-45.0, -2.0, "decreased (-45%) while the share price also fell"),
    (3.0, 0.5, "roughly flat (+3%) while the share price was roughly flat"),
    (None, 2.0, "attention was new this week while the share price also rose"),
])
def test_attention_vs_price_wording_is_neutral(att, price, must):
    s = market_data.describe_attention_vs_price(att, price)
    assert must in s
    assert "not evidence that one caused the other" in s
    lowered = s.lower().replace("not evidence that one caused the other", "")
    for banned in ("caused", "because", "drove", "due to", "will ", "should buy", "predict"):
        assert banned not in lowered


def test_no_price_no_sentence():
    assert market_data.describe_attention_vs_price(50.0, None) is None


def test_demo_provider_deterministic_and_chained():
    p = market_data.DemoMarketDataProvider()
    a = p.weekly_changes(["NVDA", "RKLB"], date(2026, 9, 28))
    b = p.weekly_changes(["NVDA"], date(2026, 9, 28))
    prev = p.weekly_changes(["NVDA"], date(2026, 9, 21))
    assert a["NVDA"].change_pct == b["NVDA"].change_pct and a["NVDA"].source == "demo"
    assert a["NVDA"].open_price == pytest.approx(prev["NVDA"].close_price, rel=1e-3)  # weeks chain


def test_yfinance_provider_math(monkeypatch):
    import sys
    import types
    idx = pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28", "2026-10-02"])
    frame = pd.DataFrame({("NVDA", "Close"): [99.0, 100.0, 104.0, 110.0],
                          ("BRK-B", "Close"): [400.0, 400.0, 390.0, 380.0]}, index=idx)
    fake = types.SimpleNamespace(download=lambda *a, **k: frame)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    out = market_data.YFinanceProvider().weekly_changes(["NVDA", "BRK.B"], date(2026, 9, 28))
    assert out["NVDA"].open_price == 100.0 and out["NVDA"].close_price == 110.0
    assert out["NVDA"].change_pct == 10.0
    assert out["BRK.B"].change_pct == -5.0 and out["BRK.B"].source == "yfinance"


def test_provider_selection(monkeypatch):
    monkeypatch.setattr(settings, "market_data_provider", "none")
    assert market_data.get_provider() is None
    monkeypatch.setattr(settings, "market_data_provider", "auto")
    assert market_data.get_provider().name == "demo"  # tests run in demo mode
    monkeypatch.setattr(settings, "market_data_provider", "yfinance")
    assert market_data.get_provider().name == "yfinance"


# ---------------------------------------------------------------- migrations
def test_migration_upgrades_phase1_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as c:  # a Phase-1 style posts table without `permalink`
        c.execute(text("CREATE TABLE subreddits (id INTEGER PRIMARY KEY, name VARCHAR(64) UNIQUE NOT NULL)"))
        c.execute(text("""CREATE TABLE posts (id INTEGER PRIMARY KEY, reddit_id VARCHAR(32) UNIQUE NOT NULL,
            subreddit_id INTEGER NOT NULL, author_id INTEGER, title TEXT NOT NULL, body TEXT NOT NULL,
            score INTEGER NOT NULL, num_comments INTEGER NOT NULL, created_utc DATETIME NOT NULL,
            week_start DATE NOT NULL, content_hash VARCHAR(64) NOT NULL, is_demo BOOLEAN NOT NULL)"""))
        c.execute(text("INSERT INTO subreddits VALUES (1, 'stocks')"))
        c.execute(text("INSERT INTO posts VALUES (1,'x',1,NULL,'t','',1,0,'2026-09-28','2026-09-28','h',0)"))
    applied = migrate(engine)
    assert "add column posts.permalink" in applied
    insp = inspect(engine)
    assert "permalink" in {c["name"] for c in insp.get_columns("posts")}
    assert {"mention_reasons", "weekly_prices", "llm_cache"} <= set(insp.get_table_names())
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM posts")).scalar() == 1  # data kept
    assert migrate(engine) == []  # idempotent


# ---------------------------------------------------------------- scheduler
def test_scheduler_next_run_is_monday_morning_in_report_tz(monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "report_timezone", "Europe/Berlin")
    sched = main._start_scheduler()
    try:
        nxt = sched.get_job("weekly_analysis").next_run_time
        assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (6, 0)
        assert str(nxt.tzinfo) == "Europe/Berlin"
    finally:
        sched.shutdown(wait=False)


def test_scheduler_off_by_default():
    from app import main
    assert main._start_scheduler() is None
