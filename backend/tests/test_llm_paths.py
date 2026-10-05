"""The LLM branches, exercised with a fake client (no network, no key needed)."""
import json

import pytest

from app import disambiguation, llm, sentiment, summary
from app.config import settings
from app.extraction import Extractor


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_batch_size", 2)
    calls = []

    def install(fn):
        def fake(model, system, user, max_tokens=2048):
            calls.append((model, json.loads(user)))
            return fn(json.loads(user))
        monkeypatch.setattr(llm, "complete_json", fake)
        return calls
    return install


def test_sentiment_llm_batched(llm_on):
    calls = llm_on(lambda items: [{"id": i["id"], "sentiment": "bearish", "confidence": 0.9} for i in items])
    res = sentiment.classify([("NVDA", "NVDA to the moon", "NVDA")] * 5)
    assert len(calls) == 3  # 5 items, batch size 2
    assert all(r.label == "bearish" and r.method == "llm" for r in res)
    assert calls[0][0] == settings.llm_fast_model


def test_sentiment_llm_failure_falls_back(llm_on):
    def boom(_):
        raise RuntimeError("rate limited")
    llm_on(boom)
    [r] = sentiment.classify([("NVDA", "NVDA is going to explode", "NVDA")])
    assert r.method == "fallback_keywords" and r.label == "bullish"


def test_sentiment_llm_bad_label_falls_back_per_item(llm_on):
    llm_on(lambda items: [{"id": 0, "sentiment": "very happy"}])
    [r] = sentiment.classify([("NVDA", "NVDA is ridiculously overvalued", "NVDA")])
    assert r.method == "fallback_keywords" and r.label == "bearish"


def test_disambiguation_llm(llm_on):
    llm_on(lambda items: [{"id": i["id"], "is_stock": i["term"] != "Ford", "confidence": 0.95} for i in items])
    ex = Extractor()
    cands = [c for t in ("Ford said hi", "SOFI calls") for c in ex.extract(t).ambiguous]
    d = disambiguation.resolve(cands)
    assert [x.is_stock for x in d] == [False, True]
    assert all(x.method == "disambiguated_llm" for x in d)


def test_summary_llm_and_shape_validation(llm_on):
    overview = {"total_mentions": 1, "stocks_detected": 1, "posts": 1, "comments": 0, "unique_authors": 1,
                "sentiment": {}}
    calls = llm_on(lambda p: {"data": ["a"], "interpretation": ["b"], "speculation": ["c"]})
    out, method = summary.generate(overview, [])
    assert method == "llm" and out["data"] == ["a"] and out["disclaimer"]
    assert calls[0][0] == settings.llm_summary_model

    llm_on(lambda p: {"data": ["only data"]})  # missing sections -> template
    out, method = summary.generate(overview, [])
    assert method == "template"


def test_extract_json_handles_fences_and_prose():
    assert llm._extract_json('```json\n[{"id": 1}]\n```') == [{"id": 1}]
    assert llm._extract_json('Sure! Here you go: [{"id": 2}] hope that helps') == [{"id": 2}]
