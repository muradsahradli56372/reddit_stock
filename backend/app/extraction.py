"""Deterministic stock-mention extraction.

Three detection methods, each with a base confidence:

  cashtag   "$NVDA"              0.97  (explicit; even "$AI" counts, at 0.90)
  alias     "Nvidia", "Rocket Lab" 0.85  (company name / alias from data/companies.csv)
  ticker    "NVDA"               0.80  (bare uppercase, 2-5 letters; 0.70 for 2 letters)

Rejections / special handling:
  * Bare uppercase words on the blacklist ("AI", "IT", "ALL", "DD", "CEO", ...) are rejected.
  * Single-letter tickers ("A", "F", "T") are only accepted as cashtags.
  * Terms in data/ambiguous.txt ("Ford", "Sofi"/"SOFI", "Apple", "Target") are NOT accepted
    directly: they go to the ambiguous queue (needs_disambiguation=True) and are resolved by
    app.disambiguation (LLM, or a rule-based fallback). Mixed-case ambiguous terms must be
    written capitalized, so "price target" or "an apple" never even become candidates.

One text can mention a ticker several times; we keep a single candidate per ticker (the most
confident one). Counting "one mention per ticker per post/comment" is a deliberate choice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .reference import Reference, get_reference

CONF_CASHTAG = 0.97
CONF_CASHTAG_BLACKLISTED = 0.90
CONF_ALIAS = 0.85
CONF_TICKER = 0.80
CONF_TICKER_SHORT = 0.70
CONF_AMBIGUOUS = 0.50

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)(?![A-Za-z0-9])")
UPPER_RE = re.compile(r"(?<![A-Za-z0-9$])([A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\n])\s+")


@dataclass
class Candidate:
    ticker: str
    method: str  # cashtag | alias | ticker | ambiguous
    confidence: float
    matched_text: str
    context: str
    needs_disambiguation: bool = False


@dataclass
class Rejection:
    token: str
    reason: str  # blacklisted | single_letter


@dataclass
class ExtractionResult:
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.needs_disambiguation]

    @property
    def ambiguous(self) -> list[Candidate]:
        return [c for c in self.candidates if c.needs_disambiguation]


def context_around(text: str, start: int, end: int, max_len: int = 400) -> str:
    """The sentence containing the match (plus the next one if it is very short)."""
    spans, pos = [], 0
    for part in SENTENCE_SPLIT_RE.split(text):
        idx = text.find(part, pos)
        spans.append((idx, idx + len(part)))
        pos = idx + len(part)
    for i, (s, e) in enumerate(spans):
        if s <= start < e or (start >= s and end <= e):
            ctx = text[s:e]
            if len(ctx) < 60 and i + 1 < len(spans):
                ctx = text[s:spans[i + 1][1]]
            ctx = ctx.strip()
            if len(ctx) > max_len:  # very long sentence: centre a window on the match
                rel = start - s
                lo = max(0, rel - max_len // 2)
                ctx = ctx[lo:lo + max_len]
            return ctx
    return text[max(0, start - 150): end + 150].strip()


class Extractor:
    def __init__(self, ref: Reference | None = None):
        self.ref = ref or get_reference()
        # Case-sensitive ambiguous terms (ALL CAPS, e.g. "SOFI") vs. case-insensitive ones ("Ford").
        self.amb_upper = {t: tk for t, tk in self.ref.ambiguous.items() if t.isupper()}
        self.amb_ci = {t.lower(): tk for t, tk in self.ref.ambiguous.items() if not t.isupper()}
        self.amb_tickers_upper = set(self.amb_upper)

        alias_map: dict[str, str] = {}
        for c in self.ref.companies.values():
            for a in (c.name, *c.aliases):
                # Skip very short aliases and aliases that are actually ambiguous terms.
                if len(a) >= 3 and a.lower() not in self.amb_ci:
                    alias_map.setdefault(a.lower(), c.ticker)
        self.alias_map = alias_map

        terms = sorted(set(alias_map) | set(self.amb_ci), key=len, reverse=True)
        self.alias_re = re.compile(
            r"(?<![A-Za-z0-9$])(" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9])",
            re.IGNORECASE,
        )

    def extract(self, text: str) -> ExtractionResult:
        result = ExtractionResult()
        best: dict[str, Candidate] = {}

        def offer(c: Candidate) -> None:
            cur = best.get(c.ticker)
            if cur is None:
                best[c.ticker] = c
            elif cur.needs_disambiguation and not c.needs_disambiguation:
                best[c.ticker] = c
            elif cur.needs_disambiguation == c.needs_disambiguation and c.confidence > cur.confidence:
                best[c.ticker] = c

        # 1) Cashtags: explicit, highest confidence.
        for m in CASHTAG_RE.finditer(text):
            tk = m.group(1).upper()
            if tk in self.ref.companies:
                conf = CONF_CASHTAG_BLACKLISTED if tk in self.ref.blacklist else CONF_CASHTAG
                offer(Candidate(tk, "cashtag", conf, m.group(0), context_around(text, m.start(), m.end())))

        # 2) Bare uppercase tickers.
        for m in UPPER_RE.finditer(text):
            tok = m.group(1)
            if tok not in self.ref.companies:
                if tok in self.ref.blacklist:
                    result.rejected.append(Rejection(tok, "blacklisted"))
                continue
            if tok in self.ref.blacklist:
                result.rejected.append(Rejection(tok, "blacklisted"))
                continue
            if len(tok) == 1:
                result.rejected.append(Rejection(tok, "single_letter"))
                continue
            ctx = context_around(text, m.start(), m.end())
            if tok in self.amb_tickers_upper:
                offer(Candidate(self.amb_upper[tok], "ambiguous", CONF_AMBIGUOUS, tok, ctx, True))
            else:
                conf = CONF_TICKER_SHORT if len(tok) == 2 else CONF_TICKER
                offer(Candidate(tok, "ticker", conf, tok, ctx))

        # 3) Company names / aliases.
        for m in self.alias_re.finditer(text):
            word = m.group(1)
            key = word.lower()
            ctx = context_around(text, m.start(), m.end())
            if key in self.amb_ci:
                if not word[0].isupper():  # proper nouns only: "Ford" yes, "ford" / "an apple" no
                    continue
                offer(Candidate(self.amb_ci[key], "ambiguous", CONF_AMBIGUOUS, word, ctx, True))
            else:
                offer(Candidate(self.alias_map[key], "alias", CONF_ALIAS, word, ctx))

        result.candidates = list(best.values())
        return result


_default: Extractor | None = None


def get_extractor() -> Extractor:
    global _default
    if _default is None:
        _default = Extractor()
    return _default
