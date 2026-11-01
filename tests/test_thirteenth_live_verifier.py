"""Aggregate reservations cover all clients/retries, persist across restarts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.model import ModelBudgetExceeded, ModelClient
from scripts.verify_thirteenth_live import AttemptBudget, install_budget


def test_atomic_global_limit_survives_restart(tmp_path):
    path = tmp_path/'budget.sqlite3'
    AttemptBudget(path, 7)
    def reserve(_):
        try:
            AttemptBudget(path).reserve()
            return True
        except ModelBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(20)))
    assert sum(outcomes) == 7
    assert AttemptBudget(path).status() == {'maximum': 7, 'reserved_attempts': 7}
    with pytest.raises(ModelBudgetExceeded):
        AttemptBudget(path).reserve()


@pytest.mark.asyncio
async def test_every_model_client_and_transport_retry_reserves_under_one_cap(tmp_path, monkeypatch):
    import httpx
    path = tmp_path/'budget.sqlite3'
    AttemptBudget(path, 3)
    # Restore init after the isolated harness hook, never affect another test.
    monkeypatch.setattr(ModelClient, '__init__', ModelClient.__init__)
    install_budget(path)
    local_calls, requests = [], []
    async def local_guard(): local_calls.append(1)
    def transport(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError('explicit preconnection fixture', request=request)
        return httpx.Response(200, json={'ok': True})
    config={'model_name':'explicit-budget-fixture','model_protocol':'responses',
            'model_base_url':'http://127.0.0.1:9/v1','model_allow_no_key':True,
            'model_responses_stream':False,'model_retries':1}
    first=ModelClient(config,before_request=local_guard,transport=httpx.MockTransport(transport))
    automatic=ModelClient(config,transport=httpx.MockTransport(transport))
    assert await first._post('/responses', {}) == {'ok': True}
    assert await automatic._post('/responses', {}) == {'ok': True}
    with pytest.raises(ModelBudgetExceeded): await automatic._post('/responses', {})
    assert len(requests) == 3
    assert local_calls == [1, 1]
    assert AttemptBudget(path).status()['reserved_attempts'] == 3
