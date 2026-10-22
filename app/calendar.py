"""Exchange calendar with explicit year coverage; never assumes future weekdays."""
from __future__ import annotations

import json
import os
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "calendar_2026.json"


def market_state(now: datetime | None = None, calendar_path: str | Path | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    local = now.astimezone(SHANGHAI)
    state = {"timezone": "Asia/Shanghai", "date": local.date().isoformat(), "as_of": local.isoformat(),
             "trading_day": None, "phase": "unknown", "source": None, "coverage": None}
    try:
        path = Path(calendar_path or os.environ.get("STOCK_CALENDAR_PATH") or DEFAULT_PATH)
        calendar = json.loads(path.read_text())
        state.update(source=calendar.get("source"), coverage=calendar.get("coverage"))
        coverage = calendar["coverage"]
        if not coverage["start"] <= state["date"] <= coverage["end"]:
            state["reason"] = "交易日历未覆盖该日期，请更新本地日历；不会仅按工作日启动交易任务"
            return state
        overrides = calendar.get("overrides", {})
        trading = overrides.get(state["date"], local.weekday() < 5 and state["date"] not in calendar["holidays"])
        if not isinstance(trading, bool):
            raise ValueError("交易日 override 必须为布尔值")
        state["trading_day"] = trading
        clock = local.time().replace(tzinfo=None)
        state["phase"] = "morning" if trading and time(9, 30) <= clock < time(11, 30) else "afternoon" if trading and time(13) <= clock < time(15) else "closed"
        state["reason"] = "日历覆盖范围内的交易日" if trading else "周末或交易所休市日"
    except (OSError, ValueError, KeyError, TypeError):
        state["reason"] = "交易日历缺失或格式无效"
    return state
