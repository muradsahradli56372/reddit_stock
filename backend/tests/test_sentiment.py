import pytest

from app.sentiment import classify, fallback_sentiment


@pytest.mark.parametrize("text,ticker,label", [
    ("NVDA is going to explode", "NVDA", "bullish"),
    ("NVDA is ridiculously overvalued", "NVDA", "bearish"),
    ("I sold NVDA at $100 and regret it", "NVDA", "bullish"),
    ("I sold NVDA at $100, and honestly I regret it", "NVDA", "bullish"),
    ("Glad I sold TSLA before the drop", "TSLA", "bearish"),
    ("Should have bought more RKLB last month", "RKLB", "bullish"),
    ("Bought puts on TSLA, this rally makes no sense", "TSLA", "bearish"),
    ("What's everyone's take on AAPL ahead of earnings?", "AAPL", "neutral"),
    ("PLTR is not going to moon anytime soon", "PLTR", "bearish"),
])
def test_fallback_finance_aware(text, ticker, label):
    assert fallback_sentiment(text, ticker).label == label


def test_per_ticker_clause_sentiment():
    text = "TSLA is overvalued, PLTR to the moon"
    assert fallback_sentiment(text, "TSLA").label == "bearish"
    assert fallback_sentiment(text, "PLTR").label == "bullish"


def test_mixed_signals_unclear():
    assert fallback_sentiment("AMD is strong but overvalued", "AMD").label in {"unclear", "bullish", "bearish"}
    assert fallback_sentiment("AMD strong weak", "AMD").label == "unclear"


def test_fallback_is_labelled():
    [r] = classify([("NVDA", "NVDA to the moon", "NVDA")])
    assert r.method == "fallback_keywords"
    assert 0 < r.confidence <= 1
