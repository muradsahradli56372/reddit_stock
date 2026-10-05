"""Test setup: an isolated database and no LLM calls.

Default DB is a temporary SQLite file. To run against Postgres:
    TEST_DATABASE_URL=postgresql+psycopg://reddit:reddit@localhost:5432/reddit_stock_test pytest
"""
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["ANTHROPIC_API_KEY"] = ""  # always exercise the deterministic fallbacks in tests
os.environ["AUTO_RUN_ON_STARTUP"] = "false"
os.environ["SCHEDULER_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

ANCHOR = date(2026, 9, 28)  # fixed demo anchor week (a Monday) for reproducible tests
PREV = date(2026, 9, 21)


def reset_db():
    from app import models  # noqa: F401
    db.Base.metadata.drop_all(db.engine)
    db.Base.metadata.create_all(db.engine)


@pytest.fixture(scope="session")
def analysed_db():
    """Fresh DB with the two demo weeks processed once."""
    from app.collectors.demo import DemoCollector
    from app.pipeline import run_analysis
    reset_db()
    result = run_analysis(ANCHOR, collector=DemoCollector(anchor_week=ANCHOR))
    return result
