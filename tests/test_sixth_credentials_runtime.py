"""Request-boundary revocation through existing Runtime clients, HTTP fixtures."""
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient, ModelConfigurationError
from app.service import ResearchTools
from app.store import Store
from tests.helpers_sixth import configure_test_team


def fixture_store(tmp_path):
    store = Store(tmp_path / 'credential-runtime.sqlite3')
    store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'model_native_search': False, 'model_retries': 0, 'max_model_calls': 40,
        'analysis_timeout_seconds': 10, 'agent_timeout_seconds': 5,
        'model_request_hard_timeout_seconds': 2, 'synthesis_request_hard_timeout_seconds': 2})
    cfg = {**store.settings(), 'model_name': 'fixture-model', 'model_base_url': 'https://fixture.test/v1'}
    provider = configure_test_team(store, cfg)
    return store, provider


async def runtime_for(store, monkeypatch, handler):
    account = store.save_account(AccountInput(name='credential-fixture'))
    job = store.claim_jobs(account_id=account['id'])[0]
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event):
        pass
    tools = ResearchTools(store, job, job['config_snapshot'])
    return agents.ResearchRuntime({'account': account, 'job_id': job['id']}, job['config_snapshot'], tools, emit)


def text_response():
    return httpx.Response(200, json={'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'fixture response'}]}]})


@pytest.mark.asyncio
async def test_existing_manager_client_keeps_rotated_key_but_revoke_blocks_next_http(tmp_path, monkeypatch):
    store, provider = fixture_store(tmp_path)
    requests = []
    def handler(request):
        requests.append(request)
        return text_response()
    runtime = await runtime_for(store, monkeypatch, handler)
    try:
        await runtime.client.complete(runtime.manager_session, [])
        old_key = requests[0].headers['authorization']
        await store.save_provider({'api_key': 'rotated-fixture-secret'}, provider['id'], transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={'data': [{'id': 'fixture-model'}]})))
        await runtime.client.complete(runtime.manager_session, [])
        assert len(requests) == 2 and requests[-1].headers['authorization'] == old_key
        # The client exists and still has its in-memory key. Explicit removal
        # must stop its next HTTP request, not merely block new client creation.
        await store.clear_provider_credential(provider['id'])
        with pytest.raises(ModelConfigurationError, match='撤销'):
            await runtime.client.complete(runtime.manager_session, [])
        assert len(requests) == runtime.calls == 2
        assert 'rotated-fixture-secret' not in json.dumps(runtime.events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_child_followup_checks_its_own_revoked_provider_and_manager_continues(tmp_path, monkeypatch):
    store, manager_provider = fixture_store(tmp_path)
    child_provider = await store.save_provider({'api_key': 'child-fixture-secret',
        'base_url': 'https://child.fixture.test/v1', 'protocol': 'responses'}, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={'data': [{'id': 'child-model'}]})))
    store.update_agent_definition('market_researcher', {'default_binding': {'provider_id': child_provider['id'], 'model_id': 'child-model'}})
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == 'child.fixture.test':
            return httpx.Response(200, json={'output': [{'type': 'function_call', 'call_id': 'submit-child',
                'name': 'submit_report', 'arguments': json.dumps({'summary': 'fixture internal report'})}]})
        return text_response()
    runtime = await runtime_for(store, monkeypatch, handler)
    try:
        result = await runtime._dispatch('manager', 'spawn_research', {'role_id': 'market_researcher', 'task': 'initial topic'})
        child = runtime.children[result['agent_id']]
        await child.task_handle
        assert child.status == 'completed' and len(child.reports) == 1
        await store.clear_provider_credential(child_provider['id'])
        await runtime._dispatch('manager', 'follow_up', {'agent_id': child.id, 'task': 'follow up after explicit revoke'})
        await child.task_handle
        assert child.status == 'failed' and '撤销' in child.error
        assert len([r for r in requests if r.url.host == 'child.fixture.test']) == 1
        await runtime.client.complete(runtime.manager_session, [])
        assert requests[-1].url.host == 'fixture.test'
        assert store.check_team_credentials(runtime.team, 'manager') is True
        assert 'child-fixture-secret' not in json.dumps(runtime.events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_provider_credential_reflection_never_reaches_audit_or_session(tmp_path):
    from app.model import ModelProtocolError
    secret = 'fixture-reflected-secret'
    cfg = {'model_name': 'fixture', 'model_base_url': 'https://fixture.test/v1',
           'model_protocol': 'responses', 'model_native_search': False,
           '_model_api_key': secret, 'model_retries': 0}
    def reflected(request):
        return httpx.Response(200, headers={'x-request-id': secret}, json={'model': secret,
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': secret}]}]})
    client = ModelClient(cfg, transport=httpx.MockTransport(reflected))
    session = client.session('fixture', {})
    try:
        with pytest.raises(ModelProtocolError, match='凭据反射'):
            await client.complete(session, [])
        assert secret not in json.dumps(client.requests)
        assert secret not in json.dumps(session.history)
        assert client.requests[0]['request_id'] == '[REDACTED]'
    finally:
        await client.aclose()
