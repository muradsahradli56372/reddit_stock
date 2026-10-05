"""Resolves the ambiguous queue: does "Ford" / "Sofi" / "Apple" here refer to the stock?

Only ambiguous candidates reach this module (a small fraction of all mentions).
With an API key: one batched LLM call per LLM_BATCH_SIZE items (fast model).
Without: a transparent rule-based fallback based on finance vs. person/other cues.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from . import llm
from .config import settings
from .extraction import Candidate
from .reference import get_reference
from .sentiment import BEARISH, BULLISH

log = logging.getLogger(__name__)

FINANCE_CUES = re.compile(
    r"\$|%|\b(stocks?|shares?|calls?|puts?|options?|earnings|ticker|position|bought|buy|buying|sell|"
    r"sold|selling|bullish|bearish|long|short|price|valuation|dividend|market cap|revenue|guidance|"
    r"ipo|portfolio|holding|bags?|moon|rally|dip|chart|quarter|q[1-4]|eps|analysts?|upgrade|"
    r"downgrade|investors?|invest(ed|ing)?|trading|traders?|nyse|nasdaq|squeeze|yolo|tendies|"
    r"leaps|strike|expiry|premarket|after hours|after the close|reports?|buybacks?|charter|ev sales|"
    r"deliveries|loans?|members|deposits|overvalued|undervalued|bubble)\b",
    re.IGNORECASE,
)
PERSON_CUES = re.compile(
    r"\b(my|our|his|her|their)\s+(friend|buddy|coworker|co-worker|colleague|wife|husband|girlfriend|"
    r"boyfriend|neighbor|neighbour|cousin|brother|sister|uncle|aunt|boss|manager|roommate|dog|cat)\s+"
    r"(named\s+)?{term}\b"
    r"|\b(named|called|mr\.?|mrs\.?|ms\.?|dr\.?)\s+{term}\b"
    r"|\b(henry|harrison|tom|gerald|betty|edsel)\s+{term}\b"
    r"|\b{term}\s+(said|says|told|thinks|asked|mentioned|texted|called me|was telling)\b"
    r"|\b{term}\s+(pie|juice|tree|trees|orchard|cider|sauce)\b"
    r"|\b{term}'s\s+(birthday|wedding|house|party|car|dog|movie|movies|films?|kids?|mom|dad)\b",
    re.IGNORECASE,
)


@dataclass
class Decision:
    is_stock: bool
    confidence: float
    method: str  # disambiguated_llm | disambiguated_rules


def rule_based(c: Candidate) -> Decision:
    term = re.escape(c.matched_text)
    person = re.search(PERSON_CUES.pattern.replace("{term}", term), c.context, re.IGNORECASE)
    finance_hits = len(FINANCE_CUES.findall(c.context)) + len(BULLISH.findall(c.context)) + len(BEARISH.findall(c.context))
    if person:
        return Decision(False, 0.8, "disambiguated_rules")
    if c.matched_text.isupper() and len(c.matched_text) >= 3:
        # People don't write names in ALL CAPS; "SOFI" without person cues is the ticker.
        return Decision(True, 0.7 if finance_hits else 0.6, "disambiguated_rules")
    if finance_hits >= 1:
        return Decision(True, min(0.6 + 0.05 * finance_hits, 0.8), "disambiguated_rules")
    return Decision(False, 0.55, "disambiguated_rules")


SYSTEM = (
    "You disambiguate stock ticker mentions in Reddit text. For each item decide whether the term "
    "refers to the given public company / its stock (true) or to something else such as a person's "
    "name, a fruit, or a common word (false). Reply with ONLY a JSON array: "
    '[{"id": <id>, "is_stock": true|false, "confidence": 0.0-1.0}].'
)


def _llm_batch(items: list[Candidate]) -> list[Decision]:
    ref = get_reference()
    payload = [
        {"id": i, "term": c.matched_text, "ticker": c.ticker,
         "company": ref.companies[c.ticker].name, "text": c.context}
        for i, c in enumerate(items)
    ]
    data = llm.complete_json(settings.llm_fast_model, SYSTEM, json.dumps(payload), max_tokens=1500)
    by_id = {int(d["id"]): d for d in data if isinstance(d, dict) and "id" in d}
    out = []
    for i, c in enumerate(items):
        d = by_id.get(i)
        if d is None:
            out.append(rule_based(c))
        else:
            out.append(Decision(bool(d.get("is_stock")), float(d.get("confidence", 0.7)), "disambiguated_llm"))
    return out


def resolve(items: list[Candidate]) -> list[Decision]:
    if not items:
        return []
    if not settings.use_llm:
        return [rule_based(c) for c in items]
    decisions: list[Decision] = []
    size = settings.llm_batch_size
    for i in range(0, len(items), size):
        batch = items[i:i + size]
        try:
            decisions.extend(_llm_batch(batch))
        except Exception as exc:  # network, parse, rate limit -> never break the pipeline
            log.warning("LLM disambiguation failed (%s); using rule-based fallback for %d items", exc, len(batch))
            decisions.extend(rule_based(c) for c in batch)
    return decisions
