"""Per-item LLM answer cache stored in the DB (table llm_cache).

Key = sha256(task, model, input). Re-running a week, or the same copy-pasted comment appearing
again, never costs a second LLM call. Uses the caller's session so it joins the pipeline's
transaction (important for SQLite, which allows one writer at a time).
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import upsert
from .models import LLMCache


def make_key(task: str, model: str, text: str) -> str:
    return hashlib.sha256(f"{task}\x00{model}\x00{text}".encode("utf-8")).hexdigest()


class Cache:
    def __init__(self, session: Session | None):
        self.session = session

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        if not self.session or not keys:
            return {}
        out = {}
        uniq = list(set(keys))
        for i in range(0, len(uniq), 500):
            rows = self.session.execute(select(LLMCache.key, LLMCache.value)
                                        .where(LLMCache.key.in_(uniq[i:i + 500]))).all()
            out.update(dict(rows))
        return out

    def put_many(self, task: str, items: dict[str, dict]) -> None:
        if not self.session or not items:
            return
        upsert(self.session, LLMCache, [{"key": k, "task": task, "value": v} for k, v in items.items()], ["key"])
