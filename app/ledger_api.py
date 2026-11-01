"""HTTP boundary for manual virtual fills; no broker or model execution."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi import Header

from .domain import AccountInput, HoldingInput, Preferences, symbol as validate_symbol
from .symbols import SymbolResolutionError


class LedgerMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    expected_version: int = Field(ge=1)


class TradeInput(LedgerMutation):
    fee_plan_version: int = Field(ge=1)
    symbol: str = Field(max_length=20)
    side: Literal['buy', 'sell']
    quantity: str = Field(max_length=40)
    price: str = Field(max_length=40)
    traded_at: str = Field(max_length=60)
    mode: Literal['simulated', 'historical'] = 'simulated'
    note: str = Field(default='', max_length=1000)
    fees_override: dict | None = None


class FundsInput(LedgerMutation):
    kind: Literal['opening', 'deposit', 'withdraw']
    amount: str = Field(max_length=40)
    reason: str = Field(min_length=1, max_length=1000)


class CorrectionHolding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(max_length=20)
    quantity: str | None = Field(default=None, max_length=40)
    # A returned, derived unit price can exceed the user-input scale. Only the
    # ledger may accept it as unchanged; changed values still pass dec(scale=6).
    cost_price: str | None = Field(default=None, max_length=80)
    total_cost: str | None = Field(default=None, max_length=80)
    available_quantity: str | None = Field(default=None, max_length=40)
    remove: bool = False

    _symbol = field_validator('symbol')(validate_symbol)


class CorrectionInput(LedgerMutation):
    cash: str | None = Field(default=None, max_length=40)
    holdings: list[CorrectionHolding] = Field(default_factory=list, max_length=300)
    mode: Literal['patch', 'snapshot'] = 'patch'
    preview_token: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class AccountUpdateHolding(HoldingInput):
    @field_validator('cost_price', mode='before')
    @classmethod
    def cost_check(cls, value):
        from .ledger import dec, text
        if value is None or value == '':
            return None
        if len(str(value)) > 80:
            raise ValueError('校正数值过长')
        return text(dec(value, scale=60))


class AccountUpdateInput(AccountInput):
    """Legacy full snapshot boundary; precision is checked against ledger facts."""
    holdings: list[AccountUpdateHolding] = Field(default_factory=list, max_length=300)


class ReversalInput(LedgerMutation):
    reason: str = Field(min_length=1, max_length=1000)


class FeeAdjustmentInput(ReversalInput):
    fees: dict[str, str]


class MetadataInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    preferences: Preferences
    expected_version: int = Field(ge=1)


def register_ledger_routes(app, store, service, resolver):
    @app.patch('/api/accounts/{account_id}')
    async def metadata(account_id: str, data: MetadataInput,
                       x_operation_id: str | None = Header(default=None, min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')):
        updated = store.save_account_metadata_operation(account_id, data.model_dump(), x_operation_id)
        return {**service.account(updated['id']), 'save_receipt': updated['save_receipt']}

    @app.get('/api/accounts/{account_id}/ledger')
    async def ledger(account_id: str):
        return store.ledger(account_id)

    @app.get('/api/accounts/{account_id}/fee-plan')
    async def fee_plan(account_id: str):
        return store.fee_plan(account_id)

    @app.put('/api/accounts/{account_id}/fee-plan')
    async def save_fee_plan(account_id: str, data: dict):
        return store.save_fee_plan(account_id, data)

    async def trade_payload(account_id, data):
        store.account(account_id)
        resolution = await resolver.resolve([data.symbol])
        if not resolution['ok'] or resolution['items'][0]['status'] != 'resolved':
            raise SymbolResolutionError(resolution['items'])
        return {**data.model_dump(exclude_none=True), 'symbol': resolution['symbols'][0]}

    @app.post('/api/accounts/{account_id}/trades/preview')
    async def preview_trade(account_id: str, data: TradeInput):
        return store.preview_trade(account_id, await trade_payload(account_id, data))

    @app.post('/api/accounts/{account_id}/trades', status_code=201)
    async def record_trade(account_id: str, data: TradeInput):
        return store.record_trade(account_id, await trade_payload(account_id, data))

    @app.get('/api/accounts/{account_id}/ledger-operations/{operation_id}')
    async def operation(account_id: str, operation_id: str):
        return store.ledger_operation(account_id, operation_id)

    @app.post('/api/accounts/{account_id}/funds', status_code=201)
    async def funds(account_id: str, data: FundsInput):
        return store.adjust_funds(account_id, data.model_dump())

    async def correction_payload(account_id, data):
        store.account(account_id)
        resolution = await resolver.resolve([h.symbol for h in data.holdings])
        if not resolution['ok']:
            raise SymbolResolutionError(resolution['items'])
        holdings, seen = [], set()
        for holding, item in zip(data.holdings, resolution['items']):
            sid = item['symbol']
            if sid in seen:
                raise ValueError('同一证券只能校正一行，请合并后重新预览')
            seen.add(sid)
            holdings.append({**holding.model_dump(exclude_unset=True), 'symbol': sid})
        return {**data.model_dump(exclude_unset=True),
                **({'holdings': holdings} if 'holdings' in data.model_fields_set else {})}

    @app.post('/api/accounts/{account_id}/corrections/preview')
    async def preview_correction(account_id: str, data: CorrectionInput):
        return store.preview_correction(account_id, await correction_payload(account_id, data))

    @app.post('/api/accounts/{account_id}/corrections', status_code=201)
    async def correction(account_id: str, data: CorrectionInput):
        if data.mode == 'snapshot' and not data.preview_token:
            raise ValueError('完整快照校正必须先预览全部差异，并携带preview_token确认')
        return store.correct_snapshot(account_id, await correction_payload(account_id, data))

    @app.post('/api/accounts/{account_id}/ledger/{entry_id}/reverse', status_code=201)
    async def reverse(account_id: str, entry_id: str, data: ReversalInput):
        return store.reverse_last_entry(account_id, entry_id, data.model_dump())

    @app.post('/api/accounts/{account_id}/ledger/{entry_id}/fees', status_code=201)
    async def adjust_fees(account_id: str, entry_id: str, data: FeeAdjustmentInput):
        return store.adjust_trade_fees(account_id, entry_id, data.model_dump())
