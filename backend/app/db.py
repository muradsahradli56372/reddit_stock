"""Database engine/session setup plus a portable upsert helper (Postgres + SQLite)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    kwargs = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # wait up to 30s for a lock instead of failing immediately
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")  # enforce FKs in SQLite
            if ":memory:" not in url:
                # WAL: the dashboard can read while the analysis writes ("database is locked" otherwise)
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=30000")
    return engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def configure(url: str) -> None:
    """Re-point the app at another database (used by tests)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    """Create tables and apply additive migrations (new columns/indexes)."""
    from .migrations import migrate
    migrate(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def upsert(session: Session, model, rows: Iterable[dict], conflict_cols: list[str],
           update_cols: list[str] | None = None) -> None:
    """INSERT ... ON CONFLICT for Postgres and SQLite.

    update_cols=None  -> DO NOTHING on conflict (keep existing row)
    update_cols=[...] -> DO UPDATE those columns
    """
    rows = list(rows)
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    insert_fn = postgresql.insert if dialect == "postgresql" else sqlite.insert
    # Chunk to stay under parameter limits.
    for i in range(0, len(rows), 500):
        stmt = insert_fn(model).values(rows[i:i + 500])
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=conflict_cols,
                set_={c: getattr(stmt.excluded, c) for c in update_cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
        session.execute(stmt)
