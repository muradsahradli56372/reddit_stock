"""FastAPI entrypoint: `uvicorn app.main:app --reload` (from backend/)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from . import db
from .api import router
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def _start_scheduler():
    """Optional weekly run (Monday 06:00 UTC) when SCHEDULER_ENABLED=true."""
    import os
    if os.getenv("SCHEDULER_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    from .pipeline import run_analysis
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_analysis, "cron", day_of_week="mon", hour=6, minute=0, id="weekly_analysis")
    sched.start()
    log.info("Scheduler started: weekly analysis every Monday 06:00 UTC")
    return sched


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    log.info("Database ready (%s). Demo mode: %s. LLM: %s",
             db.engine.url.render_as_string(hide_password=True), settings.demo_mode, settings.use_llm)
    if settings.auto_run_on_startup:
        from .models import WeeklyReport
        from .pipeline import run_analysis
        with db.session_scope() as s:
            empty = s.scalar(select(WeeklyReport.id).limit(1)) is None
        if empty:
            log.info("No reports yet: running initial analysis")
            run_analysis()
    sched = _start_scheduler()
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="Reddit Stock Intelligence", version="0.1.0", lifespan=lifespan,
              description="Research tool analysing Reddit discussion of public companies. Not trading advice.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
