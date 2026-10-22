from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated
from zoneinfo import ZoneInfo
import re

from pydantic import BaseModel, Field, field_validator, model_validator

TZ = ZoneInfo("Asia/Shanghai")


def now() -> str:
    return datetime.now(TZ).isoformat()


def decimal_text(value, *, integer=False, nullable=False) -> str | None:
    if value is None or value == "":
        if nullable:
            return None
        raise ValueError("数量为必填项，单位为股或份")
    if isinstance(value, bool):
        raise ValueError("需要有效十进制数")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("需要有效十进制数")
    if not n.is_finite() or n < 0 or n > Decimal("1000000000000"):
        raise ValueError("数值须为 0 至 1 万亿之间的有限数")
    if integer and n != n.to_integral_value():
        raise ValueError("持仓数量必须为整数股/份，不是手数")
    if not integer and n.as_tuple().exponent < -6:
        raise ValueError("最多支持六位小数")
    return format(n, "f")


def symbol(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"(SH|SZ|BJ):[0-9]{6}", value):
        raise ValueError("证券标识应为 SH:600519、SZ:000001 或 BJ:920001")
    return value


class HoldingInput(BaseModel):
    symbol: str
    quantity: str
    cost_price: str | None = None

    _symbol = field_validator("symbol")(symbol)

    @field_validator("quantity", mode="before")
    @classmethod
    def quantity_check(cls, v):
        return decimal_text(v, integer=True)

    @field_validator("cost_price", mode="before")
    @classmethod
    def cost_check(cls, v):
        return decimal_text(v, nullable=True)


class Preferences(BaseModel):
    risk: str = "balanced"
    horizon: str = "中长期"
    max_position_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    max_sector_pct: Annotated[float, Field(ge=0, le=100)] | None = None

    @field_validator("risk", "horizon")
    @classmethod
    def bounded(cls, value):
        if len(value) > 200:
            raise ValueError("偏好描述过长")
        return value


class AccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    cash: str | None = None
    preferences: Preferences = Field(default_factory=Preferences)
    holdings: list[HoldingInput] = Field(default_factory=list, max_length=300)
    expected_version: int | None = None

    @field_validator("name")
    @classmethod
    def name_check(cls, v):
        if not v.strip():
            raise ValueError("账户名称不能为空")
        return v.strip()

    @field_validator("cash", mode="before")
    @classmethod
    def cash_check(cls, v):
        return decimal_text(v, nullable=True)

    @model_validator(mode="after")
    def unique_positions(self):
        if len({h.symbol for h in self.holdings}) != len(self.holdings):
            raise ValueError("一个账户中同一证券只能有一行持仓")
        return self


class UniverseInput(BaseModel):
    watchlist: list[str] = Field(default_factory=list, max_length=500)
    candidates: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("watchlist", "candidates")
    @classmethod
    def symbols(cls, values):
        return list(dict.fromkeys(symbol(v) for v in values))


def money(n: Decimal) -> str:
    return format(n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def metrics(account: dict, quotes: dict, securities: dict) -> dict:
    positions, missing, notes, sectors = [], [], [], {}
    value, pnl, known_cost_value = Decimal(0), Decimal(0), Decimal(0)
    known_cost_positions = 0
    all_cost = True
    for h in account["holdings"]:
        s, q = securities.get(h["symbol"], {}), quotes.get(h["symbol"], {})
        qty = Decimal(h["quantity"])
        raw_price = q.get("price")
        try:
            price = Decimal(str(raw_price)) if raw_price is not None else None
            if price is not None and (not price.is_finite() or price < 0):
                price = None
        except InvalidOperation:
            price = None
        mv = qty * price if price is not None else (Decimal(0) if qty == 0 else None)
        if mv is None:
            missing.append(h["symbol"])
        else:
            value += mv
            sector = s.get("sector") or "未分类（含未穿透 ETF）"
            sectors[sector] = sectors.get(sector, Decimal(0)) + mv
        cost = Decimal(h["cost_price"]) if h.get("cost_price") is not None else None
        item_pnl = (price - cost) * qty if cost is not None and price is not None else None
        if qty > 0 and item_pnl is None:
            all_cost = False
        if item_pnl is not None:
            pnl += item_pnl
            known_cost_value += cost * qty
            if qty > 0:
                known_cost_positions += 1
        positions.append({**h, "name": s.get("name") or q.get("name") or h["symbol"],
                          "kind": s.get("kind", "unknown"), "sector": s.get("sector"),
                          "price": str(price) if price is not None else None,
                          "market_value": money(mv) if mv is not None else None,
                          "unrealized_pnl": money(item_pnl) if item_pnl is not None else None,
                          "quote": q or {"status": "missing", "as_of": None, "source": None}})
    for p in positions:
        p["weight_pct"] = money(Decimal(p["market_value"]) / value * 100) if not missing and value and p["market_value"] is not None else None
    cash = Decimal(account["cash"]) if account.get("cash") is not None else None
    if cash is None:
        notes.append("现金未录入：仅展示持仓证券市值及内部权重，不代表账户完整总资产或可用资金。")
    if not all_cost:
        notes.append("部分成本未录入或行情缺失：不计算完整持仓成本收益。")
    if missing:
        notes.append("部分证券缺少价格：已知价格市值为部分合计，不展示完整仓位权重或总资产。")
    if any(p["quote"].get("status") not in ("ok", "live", "demo") for p in positions):
        notes.append("部分行情缺失、延迟或状态待确认；估值使用已保存报价，详情见行情时间与来源。")
    notes.append("数量为持仓快照；不等于当前可卖数量。未记录成交日期，不推断 T+1 可卖量；ETF 交易规则应按具体品种确认。")
    return {"positions": positions, "securities_value": money(value) if not missing else None,
            "known_priced_value": money(value), "cash": account.get("cash"),
            "total_assets": money(value + cash) if cash is not None and not missing else None,
            "unrealized_pnl": money(pnl) if all_cost and not missing else None,
            "known_cost_pnl": money(pnl) if known_cost_positions else None,
            "known_cost_positions": known_cost_positions, "missing_prices": missing, "notes": notes,
            "sector_weights": [{"sector": s, "market_value": money(v),
                                "weight_pct": money(v / value * 100) if value and not missing else None} for s, v in sectors.items()],
            "calculated_at": now(), "weight_basis": "已录入持仓证券市值（不含现金）"}
