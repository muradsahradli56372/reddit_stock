"""Finance-aware sentiment per stock mention: bullish | neutral | bearish | unclear.

With an API key: batched LLM classification (fast model), method="llm".
Without: a keyword/pattern fallback, method="fallback_keywords". The fallback is deliberately
simple and is labelled as such everywhere it is shown. It does handle the classic traps:

  "NVDA is going to explode"            -> bullish
  "NVDA is ridiculously overvalued"     -> bearish
  "I sold NVDA at $100 and regret it"   -> bullish (regret over selling = positive on the stock)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from . import llm
from .config import settings

log = logging.getLogger(__name__)

LABELS = ("bullish", "neutral", "bearish", "unclear")

# Patterns checked first, they override plain keyword counting.
SPECIAL = [
    (re.compile(r"\b(sold|sell|selling|dumped|exited)\b.{0,60}\b(regret|kicking myself|mistake|"
                r"should(n't| not) have|shoulda held|paper hands?)\b", re.I), "bullish", 0.75),
    (re.compile(r"\b(regret|kicking myself)\b.{0,40}\b(selling|sold|not buying)\b", re.I), "bullish", 0.75),
    (re.compile(r"\b(should have|should've|shoulda|wish i had|wish i'd)\s+(bought|held|kept|loaded)", re.I),
     "bullish", 0.7),
    # Option direction beats the verb: "bought puts" is bearish, "loaded up on calls" bullish.
    (re.compile(r"\b(bought|buying|buy|loaded up on|loading up on|grabbed|got)\s+(some\s+|more\s+)?puts\b", re.I),
     "bearish", 0.7),
    (re.compile(r"\b(bought|buying|buy|loaded up on|loading up on|grabbed|got)\s+(some\s+|more\s+)?calls\b", re.I),
     "bullish", 0.7),
    (re.compile(r"\b(glad|happy|relieved)\s+i\s+(sold|got out|exited|dumped)", re.I), "bearish", 0.7),
    (re.compile(r"\b(not|never|no way|won't|isn't|aint|ain't)\b.{0,20}\b(moon|explode|recover|go up|rip)\b",
                re.I), "bearish", 0.6),
    (re.compile(r"\b(not|never|isn't|no longer)\b.{0,12}\b(overvalued|a bubble|going to crash|dead)\b", re.I),
     "bullish", 0.55),
]

BULLISH = re.compile(
    r"\b(moon(ing)?|rocket(ing)?|explode|explosive|rip(ping)?|undervalued|buy(ing)?|bought|load(ing|ed)( up)?|"
    r"adding|added|accumulat(e|ing)|calls|long|bullish|breakout|beat|crushed|strong|growth|upside|"
    r"cheap|bargain|all in|printing|tendies|going up|rally(ing)?|love|outperform|upgraded?|squeeze|"
    r"diamond hands|buy the dip|btfd|massive|huge|winner|backlog|record|game changer|lfg|higher)\b"
    r"|🚀|📈",
    re.I,
)
BEARISH = re.compile(
    r"\b(overvalued|bubble|puts|short(ing)?|bearish|sell(ing)?|dump(ing)?|crash(ing)?|tank(ing|ed)?|"
    r"drop(ping|ped)?|falling|overpriced|bag ?holders?|bag ?holding|downgraded?|miss(ed)?|weak|"
    r"decline|declining|fraud|dilution|diluting|avoid|stay away|red flags?|expensive|going down|"
    r"rug ?pull|sell-?off|terrible|losing|lost|scam|dead|trap|priced in|insane valuation|worst)\b"
    r"|📉",
    re.I,
)
CLAUSE_SPLIT = re.compile(r"[;,]|\bbut\b|\bwhile\b|\bwhereas\b|\bhowever\b", re.I)


@dataclass
class SentimentResult:
    label: str
    confidence: float
    method: str


def _focus_clause(context: str, matched_text: str) -> str:
    """The clause that contains the ticker, so 'NVDA rips, TSLA tanks' scores each separately."""
    idx = context.lower().find(matched_text.lower())
    if idx == -1:
        return context
    pos = 0
    for part in CLAUSE_SPLIT.split(context):
        if part is None:
            continue
        start = context.find(part, pos)
        if start <= idx < start + len(part):
            return part
        pos = start + len(part)
    return context


def _score(text: str) -> SentimentResult | None:
    for pattern, label, conf in SPECIAL:
        if pattern.search(text):
            return SentimentResult(label, conf, "fallback_keywords")
    bull, bear = len(BULLISH.findall(text)), len(BEARISH.findall(text))
    if bull == 0 and bear == 0:
        return None
    if bull == bear:
        return SentimentResult("unclear", 0.3, "fallback_keywords")
    label = "bullish" if bull > bear else "bearish"
    return SentimentResult(label, min(0.5 + 0.1 * abs(bull - bear), 0.8), "fallback_keywords")


def fallback_sentiment(context: str, matched_text: str) -> SentimentResult:
    clause = _focus_clause(context, matched_text)
    res = _score(clause) if clause != context else None
    if res is None:
        res = _score(context)
    return res or SentimentResult("neutral", 0.4, "fallback_keywords")


SYSTEM = (
    "You are a financial sentiment classifier for Reddit stock discussions. For each item, classify "
    "the author's stance toward the GIVEN TICKER only (not the market in general, not other tickers): "
    "bullish (expects it to rise / positive), bearish (expects it to fall / negative), neutral "
    "(factual, question, no stance) or unclear (sarcasm or mixed signals you cannot resolve). "
    "Understand finance slang: 'to the moon', 'puts', 'bagholder', 'priced in'. Regret about having "
    "sold is bullish. Reply with ONLY a JSON array: "
    '[{"id": <id>, "sentiment": "bullish|neutral|bearish|unclear", "confidence": 0.0-1.0}].'
)


def _llm_batch(items: list[tuple[str, str, str]]) -> list[SentimentResult]:
    payload = [{"id": i, "ticker": tk, "text": ctx} for i, (tk, ctx, _m) in enumerate(items)]
    data = llm.complete_json(settings.llm_fast_model, SYSTEM, json.dumps(payload), max_tokens=2000)
    by_id = {int(d["id"]): d for d in data if isinstance(d, dict) and "id" in d}
    out = []
    for i, (tk, ctx, matched) in enumerate(items):
        d = by_id.get(i)
        label = str(d.get("sentiment", "")).lower() if d else ""
        if label in LABELS:
            out.append(SentimentResult(label, float(d.get("confidence", 0.7)), "llm"))
        else:
            out.append(fallback_sentiment(ctx, matched))
    return out


def classify(items: list[tuple[str, str, str]]) -> list[SentimentResult]:
    """items: (ticker, context, matched_text). Returns one result per item, same order."""
    if not items:
        return []
    if not settings.use_llm:
        return [fallback_sentiment(ctx, m) for _tk, ctx, m in items]
    out: list[SentimentResult] = []
    size = settings.llm_batch_size
    for i in range(0, len(items), size):
        batch = items[i:i + size]
        try:
            out.extend(_llm_batch(batch))
        except Exception as exc:
            log.warning("LLM sentiment failed (%s); using keyword fallback for %d items", exc, len(batch))
            out.extend(fallback_sentiment(ctx, m) for _tk, ctx, m in batch)
    return out
