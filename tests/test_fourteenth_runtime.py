"""Concurrent HTTP reservation and opt-in role migration, using production paths."""
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app import agents
from app.agent_config import FOURTEENTH_PREVIOUS_PROMPTS, PROMPTS
from app.model import ModelClient
from app.store import Store, dumps, loads


@pytest.mark.asyncio
async def test_concurrent_children_cannot_take_two_final_http_attempts(monkeypatch):
    monkeypatch.setenv('FOURTEENTH_RUNTIME_KEY', 'fixture-only')
    config = {'model_protocol': 'responses', 'model_base_url': 'https://fixture.example/v1',
        'model_key_env': 'FOURTEENTH_RUNTIME_KEY', 'model_name': 'fixture', 'reasoning_effort': 'max',
        'model_native_search': False, 'model_retries': 0, 'max_model_calls': 6,
        'max_agent_turns': 12, 'max_concurrent_agents': 3}
    requests, events = [], []
    active = 0
    simultaneous = asyncio.Event()
    async def handler(request):
        nonlocal active
        payload = json.loads(request.content)
        seed = json.loads(payload['input'][0]['content'])
        requests.append(payload)
        if seed.get('task'):
            active += 1
            if active == 3:
                simultaneous.set()
            await asyncio.wait_for(simultaneous.wait(), 2)
            # Each child needs one repair. At most one can take remaining slot 4.
            prior = any(item.get('role') == 'assistant' for item in payload['input'])
            if not prior:
                output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '专题草稿'}]}]
            else:
                output = [{'type': 'function_call', 'call_id': 'child-final', 'name': 'submit_report',
                           'arguments': json.dumps({'summary': '已有事实支持局部观察，价格专题未完成。'})}]
        else:
            assert payload['tool_choice'] == {'type': 'function', 'name': 'finish_analysis'}
            manager_requests = [r for r in requests if not json.loads(r['input'][0]['content']).get('task')]
            if len(manager_requests) == 1:
                output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '正在整理最终报告。'}]}]
            else:
                output = [{'type': 'function_call', 'call_id': 'manager-final', 'name': 'finish_analysis', 'arguments': json.dumps({
                    'report': '现金100元，暂不投入；取得候选估值后复核。', 'decision_summary': '保留现金，取得候选估值后复核。',
                    'completion': 'limited', 'scope': ['cash_only'], 'limitations': ['候选估值未研究。']})}]
        return httpx.Response(200, json={'status': 'completed', 'output': output})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))
    async def emit(event):
        events.append(event)
    runtime = agents.ResearchRuntime({'account': {'id': 'fixture', 'version': 1, 'cash': '100', 'holdings': []}}, config, object(), emit)
    try:
        for index in range(3):
            await runtime._spawn({'task': f'Topic {index}'})
        await asyncio.gather(*list(runtime.handles))
        assert runtime.calls == len(requests) == 4
        assert sum(child.status == 'completed' for child in runtime.children.values()) == 1
        assert runtime._remaining_calls() == 2
        result = await runtime._loop('manager', runtime.manager_session, manager=True)
        assert result['submission']['call_id'] == 'manager-final'
        assert runtime.calls == len(requests) == 6
        assert sum(e['type'] == 'final_submission_candidate' and e['agent_id'] == 'manager' for e in events) == 1
    finally:
        await runtime.close()


def test_prompt_migration_only_exact_prior_builtins_and_custom_diff(tmp_path):
    store = Store(tmp_path / 'roles.sqlite3')
    old_manager = (Path(__file__).with_name('fixtures') / 'thirteenth-manager-prompt.txt').read_text()
    assert hashlib.sha256(old_manager.encode()).hexdigest() == FOURTEENTH_PREVIOUS_PROMPTS['manager']
    with store.connect() as db:
        original = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        binding = original['default_binding'].copy()
        original['system_prompt'] = old_manager
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (dumps(original),))
        custom = loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='etf_researcher'").fetchone()[0])
        custom['system_prompt'] += '\n用户自定义范围，请保留。'
        db.execute("UPDATE agent_definitions SET body=? WHERE role_id='etf_researcher'", (dumps(custom),))
        db.execute("DELETE FROM meta WHERE key='fourteenth_role_migration'")
    store = Store(tmp_path / 'roles.sqlite3')
    roles = {r['role_id']: r for r in store.list_agent_roles()}
    assert roles['manager']['system_prompt'] == (PROMPTS / 'manager.txt').read_text()
    assert roles['manager']['prompt_version'] == original['prompt_version'] + 1
    assert roles['manager']['default_binding'] == binding
    assert roles['etf_researcher']['system_prompt'] == custom['system_prompt']
    assert roles['etf_researcher']['prompt_update']['requires_explicit_save']
    assert '用户自定义范围' in roles['etf_researcher']['prompt_update']['diff']
    assert Store(tmp_path / 'roles.sqlite3').list_agent_roles() == store.list_agent_roles()
