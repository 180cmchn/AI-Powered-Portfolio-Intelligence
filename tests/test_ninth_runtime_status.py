"""Selected-account readiness and global explicit probes keep distinct identities."""
import httpx
import pytest

from app.domain import AccountInput
from app.main import create_app
from app.model import native_search_fingerprint
from tests.helpers_sixth import FixtureCredentials


@pytest.mark.asyncio
async def test_account_status_does_not_relabel_global_probe_or_default(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('MODEL_NAME', '')
    monkeypatch.setenv('MODEL_KEY_ENV', 'UNCONFIGURED_NINTH_STATUS_KEY')
    app = create_app(tmp_path / 'status.sqlite3', worker=False)
    store, service = app.state.store, app.state.service
    store._credential_store = FixtureCredentials()
    provider = await store.save_provider({'base_url': 'https://status.example.test/v1', 'protocol': 'responses', 'api_key': 'private-status-fixture'},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'data': [{'id': 'known-model'}]})))
    store.update_agent_definition('manager', {'default_binding': {'provider_id': provider['id'], 'model_id': 'known-model', 'reasoning_effort': 'medium'}})
    account = store.save_account(AccountInput(name='status fixture', holdings=[]))
    store.set_account_agent_override(account['id'], 'manager', {'reasoning_effort': 'max'})
    global_config, _ = service.manager_config(credentials=False)
    store.set_meta('native_search_capability', {'status': 'observed', 'fingerprint': native_search_fingerprint(global_config)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        status = (await client.get('/api/status', params={'account_id': account['id']})).json()
    assert status['global_manager']['reasoning_effort'] == 'medium'
    assert status['effective_manager']['reasoning_effort'] == 'max'
    assert status['configuration_readiness']['manager_binding']['reasoning_effort'] == 'max'
    assert status['native_search']['status'] == 'unverified'
    assert status['global_native_search']['status'] == 'observed'
    assert status['model_configured'] and status['global_model_configured']
