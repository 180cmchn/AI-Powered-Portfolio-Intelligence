"""Explicit snapshot corrections for legacy version/forecast regression fixtures.

These tests model a manual input correction, never an inferred buy/sell. Actual
virtual trade and funds semantics are covered by the eleventh ledger/API suites.
"""
def save_snapshot_correction(store, payload, account_id):
    data = payload.model_dump() if hasattr(payload, 'model_dump') else dict(payload)
    data.update(ledger_action='correction', adjustment_reason='受控回归夹具：明确期初/快照校正，不是买卖')
    if data.get('expected_version') is None:
        data['expected_version'] = store.account(account_id)['version']
    return store.save_account(data, account_id)


def snapshot_payload(payload):
    return {**payload, 'ledger_action': 'correction', 'adjustment_reason': '受控回归夹具：明确期初/快照校正，不是买卖'}
