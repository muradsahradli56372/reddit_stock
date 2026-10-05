"""Weekly narrative with DATA / INTERPRETATION / SPECULATION kept strictly separate.

* Template mode (no API key): every sentence is generated from computed numbers.
* LLM mode: ONE call to the stronger model with the computed metrics as JSON. The model may only
  restate/interpret the given numbers; if its output is malformed we fall back to the template.

Rules enforced in both modes: no price predictions, no causal claims ("Reddit drove the price"),
no investment advice.
"""
from __future__ import annotations

import json
import logging

from . import llm
from .config import settings
from .reasons import LABELS as REASON_LABELS

log = logging.getLogger(__name__)

DISCLAIMER = ("Research tool, not investment advice. Reddit attention and sentiment are measured "
              "signals of discussion, not predictors of price, and no causal link to price moves is implied.")


def _fmt_change(pct: float | None, has_baseline: bool = True) -> str:
    if not has_baseline:
        return "no earlier week to compare"
    return "new this week" if pct is None else f"{pct:+.0f}% WoW"


def _top_reasons(reasons: dict, ticker: str, stance: str, n: int = 2) -> str:
    items = (reasons or {}).get(ticker, {}).get(stance, [])[:n]
    return ", ".join(f"{REASON_LABELS.get(c, c).lower()} ({k})" for c, k in items)


def _communities(m: dict) -> int:
    """Text communities + Reddit subreddits (count-only) where the ticker was discussed."""
    return len(set(m.get("subreddit_distribution") or {})
               | {c for c, v in (m.get("reddit_distribution") or {}).items() if v})


def template_summary(overview: dict, metrics: list[dict], reasons: dict | None = None) -> dict:
    data, interp, spec = [], [], []
    st = overview.get("sentiment", {})
    cov = overview.get("text_coverage") or {}
    if cov and not cov.get("complete", True):
        when = f"first collected at {cov['collected_from']} UTC" if cov.get("collected_from") else \
            "collected without a record of when"
        data.append(f"Text for this week was {when}, possibly after much of the week had passed; busy tickers may "
                    "be covered only for their last hours, so this week is a partial sample and is not used as a "
                    "comparison baseline.")
    if not overview.get("baseline_available", True):
        data.append("No earlier fully collected week exists yet, so week-over-week changes, trend scores and "
                    "early signals are not rated this week (shown as UNRATED). They start once one full week "
                    "has been collected.")
    if overview.get("in_progress"):
        data.append(f"Week in progress: {overview.get('days_elapsed')} of 7 days so far. Text comparisons with "
                    "the previous week are pace-adjusted (previous counts scaled to the same elapsed time).")
    data.append(
        f"{overview['total_mentions']} stock mentions across {overview['stocks_detected']} tickers, "
        f"from {overview['posts']} posts and {overview['comments']} comments by "
        f"{overview['unique_authors']} unique authors."
    )
    if st:
        data.append(f"Overall sentiment of mentions: {st['bullish']['pct']}% bullish, "
                    f"{st['bearish']['pct']}% bearish, {st['neutral']['pct']}% neutral.")
    top = metrics[:3]
    if top:
        data.append("Most discussed: " + "; ".join(
            f"{m['ticker']} ({m['mentions']} mentions, {m['unique_authors']} authors, "
            f"{_fmt_change(m['mention_change_pct'], m.get('text_baseline', True))})"
            for m in top) + ".")
    movers = sorted(
        [m for m in metrics if m["trend_class"] in ("EMERGING", "RISING")],
        key=lambda m: -m["trend_score"])
    for m in movers[:3]:
        reddit = ""
        if m.get("reddit_mentions") is not None:
            prev = "" if m.get("reddit_prev_mentions") is None else f"{m['reddit_prev_mentions']:.0f} -> "
            chg = "" if m.get("reddit_prev_mentions") is None else f" ({_fmt_change(m['reddit_change_pct'])})"
            reddit = f"Reddit mentions (est.) {prev}{m['reddit_mentions']:.0f}{chg}"
        if m["mentions"]:
            text = (f"{m['prev_mentions']} -> {m['mentions']} text mentions "
                    f"({_fmt_change(m['mention_change_pct'], m.get('text_baseline', True))}), unique authors "
                    f"{m['prev_unique_authors']} -> "
                    f"{m['unique_authors']}, across {_communities(m)} communities")
        else:
            text = "not in the text sample"
        data.append(f"{m['ticker']}: " + "; ".join(x for x in (reddit, text) if x)
                    + f"; trend score {m['trend_score']:.0f} ({m['trend_class']}).")
    for m in movers[:3]:
        if m.get("attention_vs_price"):  # the "co-occurrence, not causation" caveat lives in the disclaimer
            data.append(f"{m['ticker']}: {m['attention_vs_price'].split('. This is')[0]}.")
    early = [m for m in metrics if m.get("is_early_signal")]
    if early:
        data.append("Early signals (small but fast-broadening discussion): " + ", ".join(
            f"{m['ticker']} ({m['early_signal_score']:.0f})" for m in early[:4]) + ".")
    cooling = [m for m in metrics if m["trend_class"] == "COOLING"]
    for m in cooling[:2]:
        if m.get("reddit_prev_mentions") is not None:
            data.append(f"{m['ticker']} Reddit attention fell {m['reddit_prev_mentions']:.0f} -> "
                        f"{m['reddit_mentions']:.0f} est. mentions ({_fmt_change(m['reddit_change_pct'])}).")
        else:
            data.append(f"{m['ticker']} attention fell {m['prev_mentions']} -> {m['mentions']} mentions "
                        f"({_fmt_change(m['mention_change_pct'], m.get('text_baseline', True))}).")

    # Interpretation: explicitly derived from the numbers above.
    for m in movers[:3]:
        if not m["mentions"]:
            interp.append(f"{m['ticker']}'s rise is visible only in Reddit mention counts (across "
                          f"{_communities(m)} subreddits); it is not in the text sample, so its sentiment and the "
                          "reasons behind it are unknown.")
            continue
        breadth = "broad-based" if _communities(m) >= 3 else "concentrated in few communities"
        authors_grew = (m["author_change_pct"] is None) or (m["author_change_pct"] or 0) > 50
        interp.append(
            f"{m['ticker']}'s attention increase is {breadth}"
            + (" and driven by many new participants rather than a few accounts." if authors_grew
               else ", but unique-author growth is modest, so a smaller group is talking more.")
            + f" Sentiment among its mentions is {m['bullish_pct']:.0f}% bullish vs {m['bearish_pct']:.0f}% bearish."
        )
        bull = _top_reasons(reasons, m["ticker"], "bullish")
        bear = _top_reasons(reasons, m["ticker"], "bearish")
        if bull or bear:
            interp.append(f"{m['ticker']} discussion centres on " + "; ".join(
                x for x in [f"bullish: {bull}" if bull else "", f"bearish: {bear}" if bear else ""] if x) + ".")
    concentrated = [m for m in metrics if m["top_author_share"] >= 0.25 and m["mentions"] >= 10]
    for m in concentrated[:2]:
        interp.append(f"{m['ticker']}'s volume is concentrated: a single account produced "
                      f"{m['top_author_share'] * 100:.0f}% of its mentions, so raw mention counts overstate "
                      f"how many people are discussing it ({m['unique_authors']} unique authors).")
    for m in cooling[:2]:
        bear = _top_reasons(reasons, m["ticker"], "bearish")
        interp.append(f"Discussion of {m['ticker']} cooled; bearish share is {m['bearish_pct']:.0f}% of mentions"
                      + (f", most often citing {bear}." if bear else "."))
    if not interp:
        interp.append("No stock showed attention growth clearly above its own baseline this week.")

    # Speculation: hypotheses only, clearly conditional.
    for m in movers[:2]:
        spec.append(f"If {m['ticker']}'s attention stays elevated next week, it may reflect a persisting "
                    f"narrative rather than a one-off event. This is a hypothesis about discussion, not about price.")
    spec.append("Sentiment labels can miss sarcasm and irony common on Reddit; treat sentiment shifts "
                "as tentative until they persist for several weeks.")
    return {"data": data, "interpretation": interp, "speculation": spec, "disclaimer": DISCLAIMER}


SYSTEM = (
    "You write a weekly research note about Reddit discussion of public companies. You receive "
    "pre-computed metrics as JSON; they are the ONLY facts you may use. Never invent numbers. Never "
    "predict prices, never give investment advice, and never claim Reddit caused a price move. "
    "Return ONLY JSON: {\"data\": [..], \"interpretation\": [..], \"speculation\": [..]} where each "
    "value is a list of 2-6 short sentences. 'data' restates measured numbers exactly. "
    "'interpretation' explains what the numbers suggest (e.g. breadth, concentration, sentiment). "
    "'speculation' contains clearly hedged hypotheses (use 'may', 'could', 'if'). Two kinds of data: "
    "'reddit_*' fields are estimated Reddit mention COUNTS (no text); 'mentions', 'unique_authors' and "
    "sentiment come from a TEXT sample (overview.text_platforms says which platform). A stock with "
    "mentions = 0 has Reddit counts only: never state its sentiment or reasons."
)


def generate(overview: dict, metrics: list[dict], reasons: dict | None = None) -> tuple[dict, str]:
    """Returns (summary_dict, method) where method is 'llm' or 'template'."""
    if settings.use_llm:
        keep = ("ticker", "mentions", "unique_authors", "prev_mentions", "mention_change_pct",
                "author_change_pct", "bullish_pct", "bearish_pct", "subreddit_count",
                "top_author_share", "trend_score", "trend_class", "early_signal_score", "is_early_signal",
                "price_change_pct", "attention_vs_price", "reddit_mentions", "reddit_prev_mentions",
                "reddit_change_pct")
        ov = {k: v for k, v in overview.items() if k != "collection"}
        stocks = []
        for m in metrics[:15]:
            d = {k: m.get(k) for k in keep}
            r = (reasons or {}).get(m["ticker"])
            if r:
                d["reasons"] = {st: [{"reason": REASON_LABELS.get(c, c), "mentions": n} for c, n in v[:3]]
                                for st, v in r.items()}
            stocks.append(d)
        payload = {"overview": ov, "stocks": stocks}
        try:
            out = llm.complete_json(settings.llm_summary_model, SYSTEM, json.dumps(payload, default=str), 1500)
            if all(isinstance(out.get(k), list) and out[k] for k in ("data", "interpretation", "speculation")):
                out = {k: [str(s) for s in out[k]] for k in ("data", "interpretation", "speculation")}
                out["disclaimer"] = DISCLAIMER
                return out, "llm"
            log.warning("LLM summary had unexpected shape; using template")
        except Exception as exc:
            log.warning("LLM summary failed (%s); using template", exc)
    return template_summary(overview, metrics, reasons), "template"
