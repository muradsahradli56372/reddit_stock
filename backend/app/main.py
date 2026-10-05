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
from .logging_setup import setup_logging

setup_logging(settings.log_format, settings.log_level)
log = logging.getLogger("app")


def scheduled_run():
    from .pipeline import run_analysis
    try:
        result = run_analysis()
        log.info("scheduled analysis finished", extra={"weeks": [w["week_start"] for w in result["weeks"]]})
    except Exception:
        log.exception("scheduled analysis failed")


def scheduled_collect():
    from .pipeline import collect_now
    try:
        collect_now()
    except Exception:
        log.exception("scheduled collection failed")


def _start_scheduler():
    """Weekly run (default Monday 06:00 in REPORT_TIMEZONE) when SCHEDULER_ENABLED=true."""
    if not settings.scheduler_enabled:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BackgroundScheduler(timezone=settings.report_timezone)
    trigger = CronTrigger.from_crontab(settings.schedule_cron, timezone=settings.report_timezone)
    sched.add_job(scheduled_run, trigger, id="weekly_analysis", max_instances=1, coalesce=True,
                  misfire_grace_time=3600)
    # Count-only sources (ApeWisdom) keep no history and StockTwits is read incrementally,
    # so a light collection job must run at least daily.
    sched.add_job(scheduled_collect, CronTrigger.from_crontab(settings.collect_cron, timezone=settings.report_timezone),
                  id="collect", max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.start()
    log.info("scheduler started", extra={"cron": settings.schedule_cron, "tz": settings.report_timezone,
                                         "next_run": str(sched.get_job("weekly_analysis").next_run_time),
                                         "collect_cron": settings.collect_cron})
    return sched


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    log.info("startup", extra={"db": db.engine.url.render_as_string(hide_password=True),
                               "demo_mode": settings.demo_mode, "llm": settings.use_llm,
                               "timezone": settings.report_timezone})
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


app = FastAPI(title="Reddit Stock Intelligence", version="0.3.0", lifespan=lifespan,
              description="Research tool analysing Reddit discussion of public companies. Not trading advice.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
