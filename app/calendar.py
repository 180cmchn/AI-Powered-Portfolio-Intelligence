"""Exchange calendar with explicit year coverage; never assumes future weekdays."""
from __future__ import annotations

import json
import os
import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "calendar_2026.json"


def _calendars(calendar_path=None):
    """A directory of verified annual files may extend coverage without code edits.

    Files must each state their own coverage. Gaps never turn into guessed weekdays.
    STOCK_CALENDAR_PATH accepts one file or a directory containing calendar_*.json.
    """
    path = Path(calendar_path or os.environ.get("STOCK_CALENDAR_PATH") or DEFAULT_PATH)
    paths = sorted(path.glob("calendar_*.json")) if path.is_dir() else [path]
    if not paths:
        raise ValueError("交易日历目录为空")
    return [json.loads(p.read_text()) for p in paths]


def calendar_revision(calendar_path=None):
    """Content identity also detects corrections without a manually bumped label."""
    try:
        value = _calendars(calendar_path)
    except (OSError, ValueError, KeyError, TypeError):
        value = {"status": "unavailable"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def trading_date(day: str | date, calendar_path=None) -> dict:
    day = date.fromisoformat(str(day))
    stamp = day.isoformat()
    for calendar in _calendars(calendar_path):
        coverage = calendar["coverage"]
        if coverage["start"] <= stamp <= coverage["end"]:
            if "trading_dates" in calendar:
                trading = stamp in calendar["trading_dates"]
            else:
                trading = day.weekday() < 5 and stamp not in calendar["holidays"]
            trading = calendar.get("overrides", {}).get(stamp, trading)
            if not isinstance(trading, bool):
                raise ValueError("交易日 override 必须为布尔值")
            return {"date": stamp, "trading_day": trading, "coverage": coverage,
                    "source": calendar.get("source"), "calendar_version": calendar.get("version", calendar.get("verified_at")),
                    "calendar_digest": hashlib.sha256(json.dumps(calendar, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    return {"date": stamp, "trading_day": None, "coverage": None,
            "reason": "交易日历未覆盖该日期，请补充交易所已发布的日历"}


def advance_trading_days(base_date: str, horizon: int, calendar_path=None) -> dict:
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 1000:
        raise ValueError("预测期限须为 1 至 1000 个交易日的整数")
    base = trading_date(base_date, calendar_path)
    if base["trading_day"] is not True:
        return {"target_date": None, "status": "calendar_gap", "reason": base.get("reason", "基准日不是交易日")}
    day, counted, sources = date.fromisoformat(base_date), 0, []
    while counted < horizon:
        day += timedelta(days=1)
        item = trading_date(day, calendar_path)
        if item["trading_day"] is None:
            return {"target_date": None, "status": "calendar_gap", "missing_date": day.isoformat(), "reason": item["reason"]}
        if item.get("source") not in sources:
            sources.append(item.get("source"))
        counted += int(item["trading_day"])
    return {"target_date": day.isoformat(), "status": "ready", "sources": sources, "base_excluded": True}


def is_after_close(at: datetime, calendar_path=None) -> bool:
    local = at.replace(tzinfo=SHANGHAI) if at.tzinfo is None else at.astimezone(SHANGHAI)
    return trading_date(local.date(), calendar_path)["trading_day"] is True and local.time().replace(tzinfo=None) >= time(15)


def market_state(now: datetime | None = None, calendar_path: str | Path | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    local = now.astimezone(SHANGHAI)
    state = {"timezone": "Asia/Shanghai", "date": local.date().isoformat(), "as_of": local.isoformat(),
             "trading_day": None, "phase": "unknown", "session": "unknown", "source": None, "coverage": None,
             "is_continuous_trading": False, "has_closed_today": None, "quote_trigger_eligible": False}
    try:
        item = trading_date(local.date(), calendar_path)
        state.update(source=item.get("source"), coverage=item.get("coverage"), calendar_version=item.get("calendar_version"),
                     calendar_digest=item.get("calendar_digest"))
        if item["trading_day"] is None:
            state["reason"] = item["reason"]
            return state
        trading = item["trading_day"]
        state["trading_day"] = trading
        clock = local.time().replace(tzinfo=None)
        state["phase"] = "morning" if trading and time(9, 30) <= clock < time(11, 30) else "afternoon" if trading and time(13) <= clock < time(15) else "closed"
        state["session"] = ("non_trading_day" if not trading else "pre_open" if clock < time(9, 30)
                            else "morning" if clock < time(11, 30) else "lunch_break" if clock < time(13)
                            else "afternoon" if clock < time(15) else "after_close")
        state["is_continuous_trading"] = state["session"] in ("morning", "afternoon")
        state["has_closed_today"] = trading and clock >= time(15)
        state["quote_trigger_eligible"] = state["is_continuous_trading"]
        state["reason"] = {"lunch_break": "午间正常休市，尚未到当日收盘；展示报价不代表可触发价格条件",
                           "pre_open": "开盘前，尚未进入连续交易", "after_close": "已到当日收盘；精确收盘仍须数据源确认",
                           "non_trading_day": "周末或交易所休市日"}.get(state["session"], "日历覆盖范围内的连续交易时段")
    except (OSError, ValueError, KeyError, TypeError):
        state["reason"] = "交易日历缺失或格式无效"
    return state
