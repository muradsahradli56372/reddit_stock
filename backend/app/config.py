"""Central configuration, read from environment variables (and an optional .env file).

Everything has a safe default so the app runs in demo mode with zero setup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

DEFAULT_SQLITE_URL = f"sqlite:///{ROOT_DIR / 'data' / 'reddit_stock.db'}"


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL") or DEFAULT_SQLITE_URL)
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR") or ROOT_DIR / "data"))

    # Reddit (Phase 2 live collector). Missing credentials => demo mode.
    reddit_client_id: str = field(default_factory=lambda: os.getenv("REDDIT_CLIENT_ID", ""))
    reddit_client_secret: str = field(default_factory=lambda: os.getenv("REDDIT_CLIENT_SECRET", ""))
    reddit_user_agent: str = field(default_factory=lambda: os.getenv("REDDIT_USER_AGENT", "reddit-stock-intel/0.1"))
    subreddits: list[str] = field(default_factory=lambda: _list(
        "SUBREDDITS",
        "stocks,wallstreetbets,investing,StockMarket,options,SecurityAnalysis,ValueInvesting,Daytrading",
    ))
    force_demo: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))
    reddit_max_posts_per_sub: int = field(default_factory=lambda: int(os.getenv("REDDIT_MAX_POSTS_PER_SUB", "500")))
    reddit_replace_more_limit: int = field(default_factory=lambda: int(os.getenv("REDDIT_REPLACE_MORE_LIMIT", "8")))
    reddit_max_retries: int = field(default_factory=lambda: int(os.getenv("REDDIT_MAX_RETRIES", "5")))

    # Weeks are Monday 00:00 -> Sunday 23:59:59 in this timezone.
    report_timezone: str = field(default_factory=lambda: os.getenv("REPORT_TIMEZONE", "UTC"))
    # Demo mode generates this many consecutive weeks so history charts have data.
    demo_history_weeks: int = field(default_factory=lambda: int(os.getenv("DEMO_HISTORY_WEEKS", "8")))

    # Market data: auto (demo in demo mode, else yfinance) | yfinance | demo | none
    market_data_provider: str = field(default_factory=lambda: os.getenv("MARKET_DATA_PROVIDER", "auto").lower())
    market_data_top_n: int = field(default_factory=lambda: int(os.getenv("MARKET_DATA_TOP_N", "25")))

    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", False))
    # Standard 5-field cron in REPORT_TIMEZONE. Default: Mondays 06:00.
    schedule_cron: str = field(default_factory=lambda: os.getenv("SCHEDULE_CRON", "0 6 * * mon"))
    log_format: str = field(default_factory=lambda: os.getenv("LOG_FORMAT", "text").lower())  # text | json
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    api_cache_ttl: int = field(default_factory=lambda: int(os.getenv("API_CACHE_TTL", "300")))

    # Anthropic. Missing key => rule-based fallbacks everywhere.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    llm_fast_model: str = field(default_factory=lambda: os.getenv("LLM_FAST_MODEL", "claude-haiku-4-5-20251001"))
    llm_summary_model: str = field(default_factory=lambda: os.getenv("LLM_SUMMARY_MODEL", "claude-sonnet-5-5"))
    llm_batch_size: int = field(default_factory=lambda: int(os.getenv("LLM_BATCH_SIZE", "25")))
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))

    auto_run_on_startup: bool = field(default_factory=lambda: _bool("AUTO_RUN_ON_STARTUP", True))
    cors_origins: list[str] = field(default_factory=lambda: _list(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"))

    @property
    def has_reddit_credentials(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def demo_mode(self) -> bool:
        return self.force_demo or not self.has_reddit_credentials

    @property
    def use_llm(self) -> bool:
        return bool(self.anthropic_api_key) and self.llm_enabled


settings = Settings()
