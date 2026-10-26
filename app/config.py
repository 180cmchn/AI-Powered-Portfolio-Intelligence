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
        "model_native_search": os.getenv("MODEL_NATIVE_SEARCH", "true").lower() not in {"0", "false", "off"},
        "model_request_timeout_seconds": 90,
        "synthesis_request_timeout_seconds": 120,
        "synthesis_max_calls": 4, "synthesis_checkpoint_max_age_seconds": 1800,
        "quote_provider": os.getenv("QUOTE_PROVIDER", "eastmoney"),
        "quote_fallback_provider": os.getenv("QUOTE_FALLBACK_PROVIDER", "sina"),
        "max_concurrent_agents": 3, "max_model_calls": 40,
        "agent_timeout_seconds": 120, "analysis_timeout_seconds": 600,
        "max_agent_turns": 12, "max_output_tokens": 4000,
        "quote_interval_seconds": 60, "scan_interval_minutes": 5,
        "scheduled_research_enabled": True,
        "analysis_interval_minutes": 15, "close_summary_time": "15:15",
        "price_trigger_pct": "3", "quote_stale_seconds": 180,
        "network_timeout_seconds": 12, "news_queries": [],
        "debounce_seconds": 1.5,
        "max_job_attempts": 2,
        "news_interval_minutes": 30, "news_start_hour": 8, "news_end_hour": 23,
        "quote_cache_seconds": 30,
        "quote_refresh_timeout_seconds": 45, "read_cache_seconds": 300,
        "research_provider": os.getenv("RESEARCH_PROVIDER", "eastmoney"),
        "tushare_token_env": "TUSHARE_TOKEN",
    }


def db_path(mode: str) -> Path:
    return Path(os.getenv("APP_DB_PATH", str(ROOT / "data" / f"{mode}.sqlite3")))
