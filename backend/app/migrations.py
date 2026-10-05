"""Minimal schema migration: create missing tables, add missing columns and indexes.

Phase 1 databases upgrade in place on startup. This only handles ADDITIVE changes
(new tables, new nullable columns, new indexes), which is all the schema needs so far.
Renames, type changes and drops need a real migration tool (Alembic); see README.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex

from .db import Base

log = logging.getLogger(__name__)


def migrate(engine: Engine) -> list[str]:
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)  # creates only tables that don't exist yet
    applied = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing_cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                if not col.nullable:
                    raise RuntimeError(f"Cannot auto-add NOT NULL column {table.name}.{col.name}")
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}'))
                applied.append(f"add column {table.name}.{col.name}")
            existing_idx = {i["name"] for i in insp.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name not in existing_idx:
                    conn.execute(CreateIndex(idx))
                    applied.append(f"create index {idx.name}")
    for a in applied:
        log.info("migration applied", extra={"change": a})
    return applied
