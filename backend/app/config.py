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

    # --- Data sources -----------------------------------------------------------------
    # TEXT_SOURCE: where post/message TEXT comes from (full pipeline: extraction, sentiment, reasons)
    #   auto (reddit if Reddit credentials are set, else demo) | demo | stocktwits | reddit
    text_source: str = field(default_factory=lambda: os.getenv("TEXT_SOURCE", "auto").lower())
    # ATTENTION_SOURCE: Reddit mention COUNTS without text
    #   auto (demo when text source is demo, else apewisdom) | apewisdom | demo | none
    attention_source: str = field(default_factory=lambda: os.getenv("ATTENTION_SOURCE", "auto").lower())

    # ApeWisdom: which filters (mostly subreddit names) to snapshot; total = sum of these.
    apewisdom_filters: list[str] = field(default_factory=lambda: _list(
        "APEWISDOM_FILTERS", "wallstreetbets,stocks,investing,options,Daytrading"))
    # Symbols ApeWisdom reports that are futures/crypto, not stocks (e.g. ES = S&P 500 e-mini futures)
    apewisdom_exclude: set[str] = field(default_factory=lambda: {t.upper() for t in _list(
        "APEWISDOM_EXCLUDE", "ES,NQ,YM,RTY,MES,MNQ,CL,GC,SI,NG,ZB,ZN,BTC,ETH,SOL,XRP,DOGE")})
    apewisdom_max_pages: int = field(default_factory=lambda: int(os.getenv("APEWISDOM_MAX_PAGES", "3")))
    # Tickers below this estimated weekly Reddit mention count are not listed on their own.
    reddit_min_weekly_mentions: int = field(default_factory=lambda: int(os.getenv("REDDIT_MIN_WEEKLY_MENTIONS", "20")))

    # StockTwits (public, unauthenticated stream API)
    stocktwits_universe_size: int = field(default_factory=lambda: int(os.getenv("STOCKTWITS_UNIVERSE_SIZE", "30")))
    stocktwits_watchlist: list[str] = field(default_factory=lambda: _list(
        "STOCKTWITS_WATCHLIST", "NVDA,TSLA,AAPL,PLTR,AMD,MSFT,AMZN,META,GME,SOFI,RKLB,ASTS,HOOD"))
    stocktwits_max_pages: int = field(default_factory=lambda: int(os.getenv("STOCKTWITS_MAX_PAGES_PER_SYMBOL", "10")))
    stocktwits_max_requests: int = field(default_factory=lambda: int(os.getenv("STOCKTWITS_MAX_REQUESTS_PER_RUN", "180")))
    stocktwits_request_delay: float = field(default_factory=lambda: float(os.getenv("STOCKTWITS_REQUEST_DELAY", "1.0")))
    http_user_agent: str = field(default_factory=lambda: os.getenv(
        "HTTP_USER_AGENT", "Mozilla/5.0 (compatible; reddit-stock-intel/0.3; research use)"))
    # Light collection job (ApeWisdom snapshot + StockTwits incremental) - must run at least daily.
    collect_cron: str = field(default_factory=lambda: os.getenv("COLLECT_CRON", "0 */4 * * *"))

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
    def resolved_text_source(self) -> str:
        if self.force_demo:
            return "demo"
        if self.text_source == "auto":
            return "reddit" if self.has_reddit_credentials else "demo"
        return self.text_source

    @property
    def resolved_attention_source(self) -> str:
        if self.attention_source == "auto":
            return "demo" if self.resolved_text_source == "demo" else "apewisdom"
        return self.attention_source

    @property
    def demo_mode(self) -> bool:
        return self.resolved_text_source == "demo"

    @property
    def use_llm(self) -> bool:
        return bool(self.anthropic_api_key) and self.llm_enabled


settings = Settings()
