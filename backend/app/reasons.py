"""Reason taxonomy + keyword fallback.

Reasons are drawn from a FIXED list of categories so they can be counted and compared
week to week ("12 bullish mentions cite contracts/partnerships"). Free-text reasons from an
LLM would not aggregate. The LLM picks categories from this list; without a key the keyword
fallback below does, and is labelled `fallback_keywords`.

A reason is only attached when it matches the mention's stance: a bullish mention gets
bullish reasons, a bearish one bearish reasons. Neutral/unclear mentions get none.
"""
from __future__ import annotations

import re

BULLISH_REASONS: dict[str, tuple[str, str]] = {
    # category: (label, keyword regex used by the fallback)
    "earnings_growth": ("Earnings / revenue growth",
                        r"earnings beat|beat estimates|revenue growth|record revenue|growth story|member growth|"
                        r"loan originations|services revenue|commercial growth|strong quarter|guidance raise"),
    "product_catalyst": ("Product / launch catalyst",
                         r"launch|neutron|blackwell|new product|iphone cycle|satellite|robotaxi|mi\d{3}|copilot|"
                         r"rollout|first flight"),
    "contracts_partnerships": ("Contracts / partnerships",
                               r"contracts?|partnerships?|deal\b|carrier|government|aip|backlog|customer wins?"),
    "ai_datacenter_demand": ("AI / data-center demand",
                             r"data ?center|hyperscaler|capex|gpu demand|ai demand|ai spend"),
    "undervalued": ("Undervalued / cheap", r"undervalued|cheap|bargain|discount|low p/?e"),
    "momentum": ("Momentum / technicals", r"breakout|momentum|ripping|all[- ]time high|new highs|uptrend"),
    "squeeze_meme": ("Short squeeze / meme", r"squeeze|short interest|meme|apes|tendies|🚀|to the moon"),
    "capital_returns": ("Buybacks / balance sheet", r"buybacks?|cash pile|dividend|bank charter|balance sheet"),
}
BEARISH_REASONS: dict[str, tuple[str, str]] = {
    "overvaluation": ("Overvaluation", r"overvalued|bubble|priced in|expensive|insane valuation|p/?e of|overpriced"),
    "weak_results": ("Weak results / guidance",
                     r"guidance|missed|miss estimates|weak quarter|margins|slowing growth|delivery numbers|"
                     r"revenue decline"),
    "dilution": ("Dilution / financing", r"dilution|diluting|offering|convertible|raising cash"),
    "competition": ("Competition", r"competition|competitors?|losing share|market share"),
    "execution_delays": ("Delays / execution risk", r"delays?|delayed|timeline|pushed back|behind schedule"),
    "macro": ("Macro / rates", r"\bfed\b|rates|recession|tariffs?|macro|inflation"),
    "legal_regulatory": ("Legal / regulatory", r"lawsuit|\bsec\b|investigation|regulat|antitrust|probe"),
    "technical_breakdown": ("Technical breakdown", r"breakdown|broke support|death cross|downtrend|lower lows"),
}
TAXONOMY = {"bullish": BULLISH_REASONS, "bearish": BEARISH_REASONS}
LABELS = {k: v[0] for d in TAXONOMY.values() for k, v in d.items()}
_COMPILED = {stance: {cat: re.compile(rx, re.I) for cat, (_l, rx) in cats.items()}
             for stance, cats in TAXONOMY.items()}


def fallback_reasons(text: str, sentiment: str, max_reasons: int = 2) -> list[tuple[str, str]]:
    """[(stance, category)] matched by keywords, only for the mention's own stance."""
    if sentiment not in TAXONOMY:
        return []
    hits = []
    for cat, rx in _COMPILED[sentiment].items():
        m = rx.search(text)
        if m:
            hits.append((m.start(), cat))
    hits.sort()
    return [(sentiment, cat) for _pos, cat in hits[:max_reasons]]


def valid(stance: str, category: str) -> bool:
    return stance in TAXONOMY and category in TAXONOMY[stance]


def taxonomy_for_prompt() -> str:
    lines = []
    for stance, cats in TAXONOMY.items():
        lines.append(f"{stance}: " + ", ".join(f"{k} ({v[0]})" for k, v in cats.items()))
    return "\n".join(lines)
