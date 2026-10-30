"""HTTP boundary for manual virtual fills; no broker or model execution."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from fastapi import Header

from .domain import AccountInput, HoldingInput, Preferences
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


class CorrectionHolding(HoldingInput):
    available_quantity: str | None = Field(default=None, max_length=40)


class CorrectionInput(LedgerMutation):
    cash: str | None
    holdings: list[CorrectionHolding] = Field(max_length=300)
    reason: str = Field(min_length=1, max_length=1000)


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

    @app.post('/api/accounts/{account_id}/corrections', status_code=201)
    async def correction(account_id: str, data: CorrectionInput):
        account = store.account(account_id)
        resolved, resolution = await resolver.account(AccountInput(name=account['name'], cash=data.cash,
            holdings=data.holdings, expected_version=data.expected_version))
        availability = {}
        for holding, item in zip(data.holdings, resolution['items']):
            sid = item['symbol']
            if sid in availability and availability[sid] != holding.available_quantity:
                raise ValueError('同一证券重复输入的可卖依据不一致，请合并为一行')
            availability[sid] = holding.available_quantity
        return store.correct_snapshot(account_id, {**data.model_dump(),
            'cash': resolved.cash, 'holdings': [{**h.model_dump(),
                **({'available_quantity': availability[h.symbol]}
                   if availability[h.symbol] is not None else {})}
                for h in resolved.holdings]})

    @app.post('/api/accounts/{account_id}/ledger/{entry_id}/reverse', status_code=201)
    async def reverse(account_id: str, entry_id: str, data: ReversalInput):
        return store.reverse_last_entry(account_id, entry_id, data.model_dump())

    @app.post('/api/accounts/{account_id}/ledger/{entry_id}/fees', status_code=201)
    async def adjust_fees(account_id: str, entry_id: str, data: FeeAdjustmentInput):
        return store.adjust_trade_fees(account_id, entry_id, data.model_dump())
