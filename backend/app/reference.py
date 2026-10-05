"""Loads the company/ticker reference data from data/ (CSV + text lists)."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import settings


@dataclass(frozen=True)
class CompanyRef:
    ticker: str
    name: str
    exchange: str
    aliases: tuple[str, ...] = ()


@dataclass
class Reference:
    companies: dict[str, CompanyRef]
    blacklist: set[str]
    # term -> ticker. Terms that can mean a stock or something else.
    ambiguous: dict[str, str] = field(default_factory=dict)

    def get(self, ticker: str) -> CompanyRef | None:
        return self.companies.get(ticker.upper())


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_reference(data_dir: Path | None = None) -> Reference:
    data_dir = data_dir or settings.data_dir
    companies: dict[str, CompanyRef] = {}
    with open(data_dir / "companies.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = row["ticker"].strip().upper()
            aliases = tuple(a.strip() for a in (row.get("aliases") or "").split("|") if a.strip())
            companies[ticker] = CompanyRef(ticker, row["name"].strip(), row["exchange"].strip(), aliases)

    blacklist = {w.upper() for w in _read_lines(data_dir / "blacklist.txt")}
    ambiguous: dict[str, str] = {}
    for line in _read_lines(data_dir / "ambiguous.txt"):
        term, _, ticker = line.partition(",")
        if ticker.strip().upper() in companies:
            ambiguous[term.strip()] = ticker.strip().upper()
    return Reference(companies, blacklist, ambiguous)


@lru_cache(maxsize=1)
def get_reference() -> Reference:
    return load_reference()
