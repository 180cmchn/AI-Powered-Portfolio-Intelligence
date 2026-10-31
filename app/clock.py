"""One wall clock for business timestamps; elapsed-time budgets stay monotonic.

Tests override ``_utc_now`` in a scoped fixture. There is deliberately no setting,
environment variable, or HTTP endpoint capable of changing the production clock.
"""
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_datetime(tz: tzinfo = SHANGHAI) -> datetime:
    return _utc_now().astimezone(tz)
