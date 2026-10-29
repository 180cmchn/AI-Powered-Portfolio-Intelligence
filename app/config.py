from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def reasoning_effort(config: dict) -> str:
    value = config.get("reasoning_effort", "medium")
    if not isinstance(value, str) or value not in REASONING_EFFORTS:
        raise ValueError("reasoning_effort 必须为 low、medium、high、xhigh、max 中的一个小写字符串；空值无效")
    return value


def model_config_summary(config: dict) -> dict:
    """Persist the request identity, never the credential or opaque model state."""
    summary = {key: config.get(key) for key in ("model_protocol", "model_base_url", "model_name", "model_native_search")}
    summary.update({key: config.get(key) for key in ("provider_id", "provider_revision", "credential_revision", "role_id", "role_version", "prompt_version", "tool_version", "team_id", "account_id") if key in config})
    summary['model_allow_no_key'] = config.get('model_allow_no_key', False)
    if 'model_responses_stream' in config:
        summary['model_responses_stream'] = config['model_responses_stream']
    summary["reasoning_effort"] = config.get("reasoning_effort", "medium")
    if isinstance(config.get("reasoning"), dict):
        summary["reasoning"] = {key: config["reasoning"][key] for key in ("mode", "summary") if key in config["reasoning"]}
    summary["fingerprint"] = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
    return summary


def defaults() -> dict:
    return {
        "model_protocol": os.getenv("MODEL_PROTOCOL", "responses"),
        "model_base_url": os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1"),
        "model_name": os.getenv("MODEL_NAME", ""),
        "model_key_env": os.getenv("MODEL_KEY_ENV", "MODEL_API_KEY"),
        "model_native_search": os.getenv("MODEL_NATIVE_SEARCH", "true").lower() not in {"0", "false", "off"},
        "reasoning_effort": os.getenv("REASONING_EFFORT", "medium"),
        "model_request_timeout_seconds": 90,
        "model_responses_stream": True,
        "synthesis_request_timeout_seconds": 120,
        "synthesis_max_calls": 4, "synthesis_checkpoint_max_age_seconds": 1800,
        "quote_provider": os.getenv("QUOTE_PROVIDER", "eastmoney"),
        "quote_fallback_provider": os.getenv("QUOTE_FALLBACK_PROVIDER", "sina"),
        "max_concurrent_agents": 3, "max_model_calls": 40,
        "agent_timeout_seconds": 120, "analysis_timeout_seconds": 600,
        "max_agent_turns": 12, "max_output_tokens": 4000,
        "quote_interval_seconds": 60, "scan_interval_minutes": 5,
        "scheduled_research_enabled": True,
        "forecast_evaluation_batch_size": 30, "forecast_review_batch_size": 6,
        "forecast_evaluation_concurrency": 4, "forecast_request_timeout_seconds": 20,
        "forecast_cycle_timeout_seconds": 45, "forecast_review_interval_days": 7,
        "forecast_unassessable_review_days": 14, "forecast_baseline_batch_size": 40,
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
