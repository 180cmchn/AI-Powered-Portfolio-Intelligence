from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def defaults() -> dict:
    return {
        "model_protocol": os.getenv("MODEL_PROTOCOL", "responses"),
        "model_base_url": os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1"),
        "model_name": os.getenv("MODEL_NAME", ""),
        "model_key_env": os.getenv("MODEL_KEY_ENV", "MODEL_API_KEY"),
        "search_provider": os.getenv("SEARCH_PROVIDER", "disabled"),
        "search_key_env": os.getenv("SEARCH_KEY_ENV", "TAVILY_API_KEY"),
        "quote_provider": os.getenv("QUOTE_PROVIDER", "eastmoney"),
        "max_concurrent_agents": 3, "max_model_calls": 40,
        "agent_timeout_seconds": 120, "analysis_timeout_seconds": 600,
        "max_agent_turns": 12, "max_output_tokens": 4000,
        "quote_interval_seconds": 60, "scan_interval_minutes": 5,
        "analysis_interval_minutes": 15, "close_summary_time": "15:15",
        "price_trigger_pct": "3", "quote_stale_seconds": 180,
        "network_timeout_seconds": 12, "news_queries": [],
        "search_max_results": 6, "debounce_seconds": 1.5,
        "max_job_attempts": 2,
        "news_interval_minutes": 30, "news_start_hour": 8, "news_end_hour": 23,
        "news_read_sources": True, "quote_cache_seconds": 30,
        "quote_refresh_timeout_seconds": 45, "read_cache_seconds": 300,
        "research_provider": os.getenv("RESEARCH_PROVIDER", "eastmoney"),
        "tushare_token_env": "TUSHARE_TOKEN",
    }


def db_path(mode: str) -> Path:
    return Path(os.getenv("APP_DB_PATH", str(ROOT / "data" / f"{mode}.sqlite3")))
