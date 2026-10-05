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

log = logging.getLogger(__name__)

DISCLAIMER = ("Research tool, not investment advice. Reddit attention and sentiment are measured "
              "signals of discussion, not predictors of price, and no causal link to price moves is implied.")


def _fmt_change(pct: float | None) -> str:
    return "new this week" if pct is None else f"{pct:+.0f}% WoW"


def template_summary(overview: dict, metrics: list[dict]) -> dict:
    data, interp, spec = [], [], []
    st = overview.get("sentiment", {})
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
            f"{m['ticker']} ({m['mentions']} mentions, {m['unique_authors']} authors, {_fmt_change(m['mention_change_pct'])})"
            for m in top) + ".")
    movers = sorted(
        [m for m in metrics if m["trend_class"] in ("EMERGING", "RISING")],
        key=lambda m: -m["trend_score"])
    for m in movers[:3]:
        data.append(f"{m['ticker']}: {m['prev_mentions']} -> {m['mentions']} mentions "
                    f"({_fmt_change(m['mention_change_pct'])}), unique authors {m['prev_unique_authors']} -> "
                    f"{m['unique_authors']}, across {m['subreddit_count']} subreddits; trend score "
                    f"{m['trend_score']:.0f} ({m['trend_class']}).")
    cooling = [m for m in metrics if m["trend_class"] == "COOLING"]
    for m in cooling[:2]:
        data.append(f"{m['ticker']} attention fell {m['prev_mentions']} -> {m['mentions']} mentions "
                    f"({_fmt_change(m['mention_change_pct'])}).")

    # Interpretation: explicitly derived from the numbers above.
    for m in movers[:3]:
        breadth = "broad-based" if m["subreddit_count"] >= 3 else "concentrated in few subreddits"
        authors_grew = (m["author_change_pct"] is None) or (m["author_change_pct"] or 0) > 50
        interp.append(
            f"{m['ticker']}'s attention increase is {breadth}"
            + (" and driven by many new participants rather than a few accounts." if authors_grew
               else ", but unique-author growth is modest, so a smaller group is talking more.")
            + f" Sentiment among its mentions is {m['bullish_pct']:.0f}% bullish vs {m['bearish_pct']:.0f}% bearish."
        )
    concentrated = [m for m in metrics if m["top_author_share"] >= 0.25 and m["mentions"] >= 10]
    for m in concentrated[:2]:
        interp.append(f"{m['ticker']}'s volume is concentrated: a single account produced "
                      f"{m['top_author_share'] * 100:.0f}% of its mentions, so raw mention counts overstate "
                      f"how many people are discussing it ({m['unique_authors']} unique authors).")
    for m in cooling[:2]:
        interp.append(f"Discussion of {m['ticker']} cooled; bearish share is {m['bearish_pct']:.0f}% of mentions.")
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
    "'speculation' contains clearly hedged hypotheses (use 'may', 'could', 'if')."
)


def generate(overview: dict, metrics: list[dict]) -> tuple[dict, str]:
    """Returns (summary_dict, method) where method is 'llm' or 'template'."""
    if settings.use_llm:
        keep = ("ticker", "mentions", "unique_authors", "prev_mentions", "mention_change_pct",
                "author_change_pct", "bullish_pct", "bearish_pct", "subreddit_count",
                "top_author_share", "trend_score", "trend_class")
        payload = {"overview": overview, "stocks": [{k: m[k] for k in keep} for m in metrics[:15]]}
        try:
            out = llm.complete_json(settings.llm_summary_model, SYSTEM, json.dumps(payload, default=str), 1500)
            if all(isinstance(out.get(k), list) and out[k] for k in ("data", "interpretation", "speculation")):
                out = {k: [str(s) for s in out[k]] for k in ("data", "interpretation", "speculation")}
                out["disclaimer"] = DISCLAIMER
                return out, "llm"
            log.warning("LLM summary had unexpected shape; using template")
        except Exception as exc:
            log.warning("LLM summary failed (%s); using template", exc)
    return template_summary(overview, metrics), "template"
