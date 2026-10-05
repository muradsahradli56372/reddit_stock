"""Market data: provider interface, two implementations, and neutral comparison wording.

MarketDataProvider.weekly_changes(tickers, week_start) -> {ticker: PriceChange}
  * YFinanceProvider  real daily closes via yfinance (needs network access to Yahoo).
  * DemoMarketDataProvider  deterministic synthetic prices, ALWAYS labelled source="demo".

Weekly change = last close inside the week vs the last close before the week started
(i.e. previous Friday's close -> this Friday's close), so it lines up with the Reddit week.

Wording rule: we only ever describe co-occurrence ("attention increased while the price also
rose"). Never causation, never prediction.
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class PriceChange:
    ticker: str
    week_start: date
    open_price: float  # previous close before the week
    close_price: float  # last close of the week
    change_pct: float
    source: str


class MarketDataProvider(Protocol):
    name: str

    def weekly_changes(self, tickers: list[str], week_start: date) -> dict[str, PriceChange]: ...


class YFinanceProvider:
    name = "yfinance"

    @staticmethod
    def _symbol(ticker: str) -> str:
        return ticker.replace(".", "-")  # BRK.B -> BRK-B

    def weekly_changes(self, tickers: list[str], week_start: date) -> dict[str, PriceChange]:
        import yfinance as yf
        if not tickers:
            return {}
        symbols = {self._symbol(t): t for t in tickers}
        df = yf.download(list(symbols), start=week_start - timedelta(days=10), end=week_start + timedelta(days=7),
                         interval="1d", auto_adjust=True, progress=False, threads=False, group_by="ticker")
        out = {}
        for sym, tk in symbols.items():
            try:
                closes = (df[sym]["Close"] if len(symbols) > 1 else df["Close"]).dropna()
                if hasattr(closes, "columns"):  # newer yfinance returns a 1-col frame
                    closes = closes.iloc[:, 0]
                before = closes[closes.index.date < week_start]
                during = closes[(closes.index.date >= week_start) & (closes.index.date < week_start + timedelta(days=7))]
                if before.empty or during.empty:
                    continue
                o, c = float(before.iloc[-1]), float(during.iloc[-1])
                out[tk] = PriceChange(tk, week_start, round(o, 4), round(c, 4), round(100 * (c - o) / o, 2), self.name)
            except Exception as exc:  # delisted / missing symbol
                log.info("no price data", extra={"ticker": tk, "error": repr(exc)})
        return out


class DemoMarketDataProvider:
    """Synthetic random-walk prices, deterministic per (ticker, week). Not real data."""
    name = "demo"

    @staticmethod
    def _base(ticker: str) -> float:
        h = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
        return 10 + h % 400

    def weekly_changes(self, tickers: list[str], week_start: date) -> dict[str, PriceChange]:
        out = {}
        for tk in tickers:
            # Price level = base * product of weekly returns from a fixed epoch, so weeks chain consistently.
            epoch = date(2026, 1, 5)
            n_weeks = max(0, (week_start - epoch).days // 7)
            level = self._base(tk)
            for w in range(n_weeks + 1):
                rng = random.Random(f"{tk}:{w}")
                ret = rng.gauss(0.002, 0.045)
                prev, level = level, level * (1 + ret)
            out[tk] = PriceChange(tk, week_start, round(prev, 2), round(level, 2),
                                  round(100 * (level - prev) / prev, 2), self.name)
        return out


def get_provider() -> MarketDataProvider | None:
    choice = settings.market_data_provider
    if choice == "none":
        return None
    if choice == "demo" or (choice == "auto" and settings.demo_mode):
        return DemoMarketDataProvider()
    return YFinanceProvider()


def _direction(pct: float | None, threshold: float) -> int:
    if pct is None:
        return 1
    return 1 if pct > threshold else -1 if pct < -threshold else 0


def describe_attention_vs_price(attention_change_pct: float | None, price_change_pct: float | None,
                                source: str = "Reddit") -> str | None:
    """Neutral, non-causal sentence comparing attention on `source` with the price move."""
    if price_change_pct is None:
        return None
    if attention_change_pct is None:
        att = f"{source} attention was new this week"
    else:
        att = {1: f"{source} attention increased ({attention_change_pct:+.0f}%)",
               -1: f"{source} attention decreased ({attention_change_pct:+.0f}%)",
               0: f"{source} attention was roughly flat ({attention_change_pct:+.0f}%)"}[_direction(attention_change_pct, 10)]
    a_dir, p_dir = _direction(attention_change_pct, 10), _direction(price_change_pct, 1)
    verb = {1: "rose", -1: "fell", 0: "was roughly flat"}[p_dir]
    also = " also" if a_dir == p_dir and p_dir != 0 else ""
    return (f"{att} while the share price{also} {verb} ({price_change_pct:+.1f}%) over the same week. "
            "This is a co-occurrence, not evidence that one caused the other.")


def now_utc() -> datetime:
    return datetime.utcnow()
