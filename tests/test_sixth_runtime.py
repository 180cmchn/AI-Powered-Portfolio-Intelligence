"""Actual Store/worker/Runtime/ModelClient, with labeled HTTP and vault substitutes."""
import asyncio
import copy
import json

import httpx
import pytest

from app import agents
from app.agent_config import INITIAL_ROLES
from app.domain import AccountInput
from app.model import ModelClient
from app.service import ResearchTools, Service
from app.store import Store
from helpers_sixth import FixtureCredentials


async def configured(tmp_path):
    store = Store(tmp_path / 'sixth-runtime-fixture.sqlite3')
    store._credential_store = FixtureCredentials()
    store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'model_native_search': False,
        'model_retries': 0, 'model_request_timeout_seconds': 2,
        'analysis_timeout_seconds': 10, 'agent_timeout_seconds': 5,
        'max_agent_turns': 20, 'max_model_calls': 80})
    discovery = []
    def catalog(request):
        discovery.append(request)
        return httpx.Response(200, json={'data': [{'id': 'vendor/Model-A'}, {'id': 'same'}]})
    p = await store.save_provider({'api_key': 'fixture-key-A', 'base_url': 'https://a.fixture.test/v1/responses',
        'protocol': 'responses'}, transport=httpx.MockTransport(catalog))
    q = await store.save_provider({'api_key': 'fixture-key-B', 'base_url': 'https://b.fixture.test/custom/chat/completions',
        'protocol': 'chat_completions'}, transport=httpx.MockTransport(catalog))
    for role in store.list_agent_roles():
        store.update_agent_definition(role['role_id'], {'default_binding': {
            'provider_id': p['id'], 'model_id': 'vendor/Model-A', 'reasoning_effort': 'medium'}})
    a = store.save_account(AccountInput(name='Fixture account A', holdings=[], research_scope='cash_only'))
    b = store.save_account(AccountInput(name='Fixture account B', holdings=[], research_scope='cash_only'))
    for account, provider, manager_effort, child_effort in ((a, p, 'high', 'low'), (b, q, 'max', 'xhigh')):
        store.set_account_agent_override(account['id'], 'manager', {'provider_id': provider['id'],
            'model_id': 'same', 'reasoning_effort': manager_effort})
        store.set_account_agent_override(account['id'], 'market_researcher', {'provider_id': provider['id'],
            'model_id': 'vendor/Model-A', 'reasoning_effort': child_effort})
    return store, a, b, p, q, discovery


def call(name, arguments=None):
    return {'name': name, 'arguments': arguments or {}}


def seed_of(payload):
    items = payload.get('input', payload.get('messages', []))
    return next(json.loads(item['content']) for item in items if item.get('role') == 'user' and isinstance(item.get('content'), str) and item['content'].startswith('{'))


def response(request, payload, calls):
    if request.url.path.endswith('/responses'):
        return httpx.Response(200, json={'id': 'fixture-response', 'model': payload['model'],
            'reasoning': payload['reasoning'], 'status': 'completed', 'output': [
                {'type': 'function_call', 'call_id': f'{index}-{item["name"]}', 'name': item['name'],
                 'arguments': json.dumps(item['arguments'])} for index, item in enumerate(calls)]})
    return httpx.Response(200, json={'id': 'fixture-chat', 'model': payload['model'],
        'reasoning_effort': payload['reasoning_effort'], 'choices': [{'finish_reason': 'tool_calls',
            'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'{index}-{item["name"]}', 'type': 'function', 'function': {
                    'name': item['name'], 'arguments': json.dumps(item['arguments'])}} for index, item in enumerate(calls)]}}]})


@pytest.mark.asyncio
async def test_two_accounts_worker_independent_clients_roles_followup_and_frozen_edits(tmp_path, monkeypatch):
    store, a, b, p, q, discovery = await configured(tmp_path)
    assert [str(request.url) for request in discovery] == ['https://a.fixture.test/v1/models', 'https://b.fixture.test/custom/models']
    captured, manager_steps, child_calls = [], {}, {}
    edited = False
    async def handler(request):
        nonlocal edited
        payload = json.loads(request.content)
        seed = seed_of(payload)
        account_id = seed['account']['id']
        role_id = seed.get('role_id', 'manager')
        runtime = seed['runtime_status']
        agent_id = runtime['agent_id']
        captured.append({'account_id': account_id, 'role_id': role_id, 'agent_id': agent_id,
            'request': request, 'payload': copy.deepcopy(payload), 'seed': copy.deepcopy(seed)})
        if role_id != 'manager':
            child_calls[agent_id] = child_calls.get(agent_id, 0) + 1
            names = {tool.get('name') or tool.get('function', {}).get('name') for tool in payload['tools']}
            assert 'submit_report' in names and 'finish_analysis' not in names
            # A deliberately invalid provider function call must be rejected before dispatch.
            if child_calls[agent_id] == 1:
                return response(request, payload, [call('finish_analysis', {'report': 'forbidden child final',
                    'forecasts': [{'security_id': 'SZ:159278', 'horizon': 1, 'direction': 'up', 'reason': 'forbidden'}]})])
            return response(request, payload, [call('submit_report', {'summary': f'{account_id} {seed["task"]} report {child_calls[agent_id]}'})])
        assert {role['role_id'] for role in seed['agent_roles']} == {role[0] for role in INITIAL_ROLES}
        step = manager_steps.get(account_id, 0)
        if step == 0:
            manager_steps[account_id] = 1
            return response(request, payload, [call('spawn_research', {'role_id': 'market_researcher', 'task': 'first topic'}),
                call('spawn_research', {'role_id': 'market_researcher', 'task': 'second topic'})])
        children = runtime['children']
        if any(child['status'] != 'completed' for child in children):
            await asyncio.sleep(0.002)
            return response(request, payload, [call('wait_agents', {'timeout_seconds': 0.02})])
        if step == 1:
            manager_steps[account_id] = 2
            if account_id == a['id']:
                store.set_account_agent_override(a['id'], 'market_researcher', {'provider_id': q['id'],
                    'model_id': 'same', 'reasoning_effort': 'high'})
                store.update_agent_definition('market_researcher', {'system_prompt': 'New global market instructions for future tasks.'})
                await store.save_provider({'api_key': 'fixture-key-A-rotated'}, p['id'],
                    transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'data': [{'id': 'vendor/Model-A'}, {'id': 'same'}]})))
                edited = True
            return response(request, payload, [call('follow_up', {'agent_id': children[0]['agent_id'], 'task': 'Follow up under original role binding'}),
                call('spawn_research', {'role_id': 'market_researcher', 'task': 'third topic spawned after settings edit'})])
        return response(request, payload, [call('finish_analysis', {'report': f'Independent final account {account_id}'})])

    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None: ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    service = Service(store)
    await service.tick()  # Production worker dispatch, actual HTTP serialization, no direct runtime driver.
    await asyncio.gather(*service.active.values())
    assert edited
    for account, key, host, manager_effort, child_effort in ((a, 'fixture-key-A', 'a.fixture.test', 'high', 'low'),
            (b, 'fixture-key-B', 'b.fixture.test', 'max', 'xhigh')):
        job = store.job(account['latest_job_id'], events=True)
        assert job['state'] == 'completed', job.get('error')
        result = store.analyses(account['id'])[0]['result']
        assert result['report'] == f'Independent final account {account["id"]}'
        assert len(result['agents']) == 3
        assert len({agent['agent_id'] for agent in result['agents']}) == 3
        assert {agent['role_id'] for agent in result['agents']} == {'market_researcher'}
        assert sorted(agent['round'] for agent in result['agents']) == [1, 1, 2]
        assert sorted(len(agent['reports']) for agent in result['agents']) == [1, 1, 2]
        assert all(agent['status'] == 'completed' for agent in result['agents'])
        requests = [item for item in captured if item['account_id'] == account['id']]
        assert requests
        for item in requests:
            assert item['request'].headers['authorization'] == 'Bearer ' + key
            assert item['request'].url.host == host
            effort = (item['payload'].get('reasoning') or {}).get('effort', item['payload'].get('reasoning_effort'))
            assert effort == (manager_effort if item['role_id'] == 'manager' else child_effort)
            assert item['payload']['model'] == ('same' if item['role_id'] == 'manager' else 'vendor/Model-A')
            assert 'New global market instructions' not in json.dumps(item['payload'])
        errors = [event for event in job['events'] if event['type'] == 'tool_result' and event['payload'].get('tool') == 'finish_analysis' and event['payload'].get('result', {}).get('failed')]
        assert len(errors) == 3
        usage = [request for event in job['events'] if event['type'] == 'model_usage' for request in event['payload']['requests']]
        assert {request['account_id'] for request in usage} == {account['id']}
        assert {request['provider_id'] for request in usage} == {p['id'] if account == a else q['id']}
        assert all(request.get('role_id') and request.get('agent_id') for request in usage)
        assert 'fixture-key-' not in json.dumps(result)
    store.enqueue(a['id'], 'manual')
    next_job = store.claim_jobs(account_id=a['id'])[0]
    next_role = next_job['config_snapshot']['_agent_team']['roles']['market_researcher']
    assert next_role['resolved']['provider_id'] == q['id']
    assert next_role['resolved']['reasoning_effort'] == 'high'
    assert next_role['system_prompt'] == 'New global market instructions for future tasks.'
    assert store.list_agent_roles(b['id'])[0]['role_id'] in {role[0] for role in INITIAL_ROLES}
    await service.stop()


@pytest.mark.asyncio
async def test_runtime_requires_role_and_rejects_binding_override_recursion_and_unknown_role(tmp_path, monkeypatch):
    store, a, _, _, _, _ = await configured(tmp_path)
    job = store.claim_jobs(account_id=a['id'])[0]
    requests = []
    def unexpected(request):
        requests.append(request)
        return httpx.Response(500, json={'error': 'unexpected request'})
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None: ModelClient(cfg, before_request, httpx.MockTransport(unexpected)))
    async def emit(event):
        pass
    tools = ResearchTools(store, job, job['config_snapshot'])
    runtime = agents.ResearchRuntime({'account': a, 'job_id': job['id']}, job['config_snapshot'], tools, emit)
    try:
        for args in ({'task': 'missing role'}, {'role_id': 'manager', 'task': 'recursive'},
                {'role_id': 'unregistered', 'task': 'new role'},
                {'role_id': 'market_researcher', 'task': 'switch', 'model': 'another'},
                {'role_id': 'market_researcher', 'task': 'switch', 'reasoning_effort': 'max'}):
            with pytest.raises(ValueError):
                await runtime._dispatch('manager', 'spawn_research', args)
        for name in ('finish_analysis', 'spawn_research', 'list_agent_roles', 'list_forecast_targets'):
            with pytest.raises(ValueError, match='主 Agent'):
                await runtime._dispatch('malicious-child', name, {})
        assert not requests and not runtime.children
    finally:
        await runtime.close()
