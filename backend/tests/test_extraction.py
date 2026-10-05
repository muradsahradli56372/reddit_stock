import pytest

from app.disambiguation import resolve, rule_based
from app.extraction import Extractor


@pytest.fixture(scope="module")
def ex():
    return Extractor()


def tickers(ex, text):
    return {c.ticker: c for c in ex.extract(text).candidates}


def test_cashtag_high_confidence(ex):
    c = tickers(ex, "$NVDA is going to explode")["NVDA"]
    assert c.method == "cashtag" and c.confidence >= 0.95 and not c.needs_disambiguation


def test_bare_uppercase_ticker(ex):
    c = tickers(ex, "Thinking about PLTR here")["PLTR"]
    assert c.method == "ticker" and 0.7 <= c.confidence < 0.95


def test_company_alias(ex):
    found = tickers(ex, "Rocket Lab and Palantir and NVIDIA Corp all moved")
    assert {"RKLB", "PLTR", "NVDA"} <= set(found)
    assert found["RKLB"].method == "alias"


def test_lowercase_alias_matches(ex):
    assert "NVDA" in tickers(ex, "nvidia earnings tonight")


@pytest.mark.parametrize("text", [
    "AI is the future and IT budgets are ALL going there.",
    "Great DD, YOLO'd my bonus. The CEO said A lot of things.",
    "IMO the FOMO is real, EPS beat but the FED and CPI matter more",
])
def test_false_positive_words_rejected(ex, text):
    res = ex.extract(text)
    assert res.candidates == []
    assert res.rejected  # we recorded why


def test_cashtag_overrides_blacklist(ex):
    c = tickers(ex, "Bought some $AI today")["AI"]
    assert c.method == "cashtag"


def test_single_letter_requires_cashtag(ex):
    assert "F" not in tickers(ex, "I got an F on my exam")
    assert "F" in tickers(ex, "$F looks cheap")


def test_dollar_amounts_are_not_tickers(ex):
    assert tickers(ex, "I sold at $100 and $45.50") == {}


def test_one_candidate_per_ticker_per_text(ex):
    res = ex.extract("NVDA NVDA $NVDA Nvidia")
    assert len(res.candidates) == 1
    assert res.candidates[0].method == "cashtag"  # the most confident detection wins


def test_ambiguous_terms_go_to_queue(ex):
    for text, tk in [("Ford earnings beat", "F"), ("SOFI calls", "SOFI"), ("Apple reports Thursday", "AAPL")]:
        c = tickers(ex, text)[tk]
        assert c.needs_disambiguation and c.confidence == 0.5


def test_lowercase_ambiguous_terms_ignored(ex):
    assert tickers(ex, "my price target is 50, eating an apple") == {}


def test_context_is_stored(ex):
    c = tickers(ex, "Unrelated first sentence here about the weather today. Long PLTR into earnings. Bye.")["PLTR"]
    assert "Long PLTR into earnings" in c.context and "weather" not in c.context


@pytest.mark.parametrize("text,term,expected", [
    ("Ford earnings beat, shares up 5%", "F", True),
    ("My buddy Ford says I should diversify", "F", False),
    ("Harrison Ford was great in that movie", "F", False),
    ("Bought SOFI calls this morning", "SOFI", True),
    ("SOFI is ridiculously overvalued", "SOFI", True),
    ("My coworker Sofi thinks the market is rigged", "SOFI", False),
    ("Made an Apple pie this weekend", "AAPL", False),
    ("Apple buybacks are huge this quarter", "AAPL", True),
])
def test_rule_based_disambiguation(ex, text, term, expected):
    c = tickers(ex, text)[term]
    assert rule_based(c).is_stock is expected


def test_resolve_uses_fallback_without_key(ex):
    c = tickers(ex, "Ford earnings beat")["F"]
    [d] = resolve([c])
    assert d.method == "disambiguated_rules"
