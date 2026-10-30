from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# Remember only names; never keep an extra copy of credentials for diagnostics.
try:
    _PARENT_DOTENV_NAMES = json.loads(os.getenv('APP_DOTENV_ORIGIN_NAMES', '[]'))
    if not isinstance(_PARENT_DOTENV_NAMES, list):
        _PARENT_DOTENV_NAMES = []
except (TypeError, ValueError):
    _PARENT_DOTENV_NAMES = []
_SHELL_ENV_NAMES = frozenset(os.environ) - frozenset(name for name in _PARENT_DOTENV_NAMES if isinstance(name, str))
_INITIAL_DOTENV_NAMES = (frozenset(dotenv_values(ROOT / '.env')) if os.getenv('PYTHON_DOTENV_DISABLED', '').lower() not in {'1', 'true', 'yes', 'on'} else frozenset()) | frozenset(name for name in _PARENT_DOTENV_NAMES if isinstance(name, str))
load_dotenv(ROOT / ".env")


def environment_connection_source() -> dict:
    """Report presence/source and pending file edits, never values of secrets.

    dotenv deliberately keeps its normal non-override behavior. Editing .env
    does not mutate an already running worker's credential references.
    """
    enabled = os.getenv('PYTHON_DOTENV_DISABLED', '').lower() not in {'1', 'true', 'yes', 'on'}
    file_values = dotenv_values(ROOT / '.env') if enabled else {}
    key_name = os.getenv('MODEL_KEY_ENV', 'MODEL_API_KEY')
    names = ('MODEL_BASE_URL', 'MODEL_PROTOCOL', 'MODEL_KEY_ENV', key_name, 'MODEL_NAME', 'REASONING_EFFORT')
    fields = {}
    for name in names:
        present = bool(os.getenv(name))
        source = 'shell' if name in _SHELL_ENV_NAMES else 'dotenv' if name in _INITIAL_DOTENV_NAMES or name in file_values else 'default_or_process'
        file_differs = ((name in file_values and file_values[name] != os.getenv(name))
                        or (enabled and name in _INITIAL_DOTENV_NAMES and name not in _SHELL_ENV_NAMES and name not in file_values and present))
        fields[name] = {'present': present, 'source': source,
                        'file_differs': bool(file_differs),
                        'shell_overrides_file': name in _SHELL_ENV_NAMES and name in file_values and file_values[name] != os.getenv(name)}
    return {'dotenv_path': str(ROOT / '.env'), 'dotenv_enabled': enabled, 'fields': fields,
            'restart_required': any(v['file_differs'] and not v['shell_overrides_file'] for v in fields.values()),
            'note': '.env 修改需要安全重启；已有 shell 环境优先。环境连接导入不会覆盖角色或账户模型选择。'}

REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

TIMEOUT_FIELDS = ('model_first_event_timeout_seconds', 'model_stream_idle_timeout_seconds',
                  'model_request_hard_timeout_seconds', 'synthesis_request_hard_timeout_seconds')


def migrate_timeout_policy(saved: dict) -> dict:
    """Derive v2 settings for *new* work, never rewrite old job snapshots.

    The old factory 90/120 second absolute limits are replaced by the documented
    v2 factory policy. Explicit non-factory limits retain their absolute meaning.
    The old values stay present for audit/old API readers, not as idle aliases.
    """
    result = dict(saved)
    for new, old, factory, replacement in (
        ('model_request_hard_timeout_seconds', 'model_request_timeout_seconds', 90, 300),
        ('synthesis_request_hard_timeout_seconds', 'synthesis_request_timeout_seconds', 120, 360),
    ):
        if new not in result:
            previous = result.get(old, factory)
            result[new] = replacement if previous == factory else previous
    result.setdefault('model_first_event_timeout_seconds', 90)
    result.setdefault('model_stream_idle_timeout_seconds', 120)
    if not any(key in saved for key in TIMEOUT_FIELDS):
        for key, old, new in (('agent_timeout_seconds', 120, 600), ('analysis_timeout_seconds', 600, 1200)):
            if saved.get(key, old) == old:
                result[key] = new
    return result


def timeout_policy(config: dict, phase='research') -> dict:
    """Resolve one immutable request policy; legacy snapshots keep old hard caps."""
    synthesis = phase == 'synthesis'
    hard_field = 'synthesis_request_hard_timeout_seconds' if synthesis else 'model_request_hard_timeout_seconds'
    legacy = ('synthesis_request_timeout_seconds' if synthesis else 'model_request_timeout_seconds')
    fallback = config.get('model_request_timeout_seconds', 120 if synthesis else 90)
    hard = float(config.get(hard_field, config.get(legacy, fallback)))
    return {'policy_version': 2 if hard_field in config else 1,
            'first_event_seconds': float(config.get('model_first_event_timeout_seconds', 90)),
            'stream_idle_seconds': float(config.get('model_stream_idle_timeout_seconds', 120)),
            'request_hard_seconds': hard,
            'connect_seconds': 15.0, 'pool_seconds': 15.0, 'write_seconds': 30.0,
            'hard_limit_source': hard_field if hard_field in config else legacy}


def timeout_policy_warnings(config: dict) -> list[str]:
    normal, final = timeout_policy(config), timeout_policy(config, 'synthesis')
    warnings = []
    if float(config.get('agent_timeout_seconds', 600)) < normal['request_hard_seconds']:
        warnings.append('子任务总预算短于普通请求硬上限；请求以子任务剩余预算为准。')
    if float(config.get('analysis_timeout_seconds', 1200)) < final['request_hard_seconds']:
        warnings.append('账户总预算短于汇总请求硬上限；汇总以账户剩余预算为准。')
    for label, policy in (('普通请求', normal), ('汇总请求', final)):
        if policy['first_event_seconds'] > policy['request_hard_seconds']:
            warnings.append(label + '硬上限短于首事件等待；可能先耗尽总预算。')
        if policy['stream_idle_seconds'] > policy['request_hard_seconds']:
            warnings.append(label + '硬上限短于流空闲限；持续等待仍受总预算约束。')
    return warnings


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
        "model_first_event_timeout_seconds": 90,
        "model_stream_idle_timeout_seconds": 120,
        "model_request_hard_timeout_seconds": 300,
        "synthesis_request_hard_timeout_seconds": 360,
        "synthesis_max_calls": 4, "synthesis_checkpoint_max_age_seconds": 1800,
        "quote_provider": os.getenv("QUOTE_PROVIDER", "eastmoney"),
        "quote_fallback_provider": os.getenv("QUOTE_FALLBACK_PROVIDER", "sina"),
        "max_concurrent_agents": 3, "max_model_calls": 40,
        "agent_timeout_seconds": 600, "analysis_timeout_seconds": 1200,
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
