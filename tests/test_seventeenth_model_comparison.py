"""Zero-paid-call checks of the actual fixed-evidence experiment driver."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.model import ModelClient as ActualModelClient, ModelError
from scripts import verify_seventeenth_cache_comparison as experiment
from tests.test_seventeenth_model_cache import config


FIXTURE = Path(__file__).parent / 'fixtures/seventeenth-financial-signs.json'


def tool_response(name, arguments, number):
    return httpx.Response(200, json={'id': f'response-{number}', 'status': 'completed',
        'output': [{'type': 'reasoning', 'encrypted_content': 'comparison-opaque', 'summary': []},
                   {'type': 'function_call', 'call_id': f'call-{number}', 'name': name,
                    'arguments': json.dumps(arguments)}],
        'usage': {'input_tokens': 1000 * number, 'input_tokens_details': {'cached_tokens': 100 * number}}})


@pytest.mark.asyncio
async def test_comparison_uses_actual_client_stable_keys_useful_tools_and_stops_at_submit(monkeypatch):
    captured, clients = {}, []
    monkeypatch.setattr(experiment, 'source_digest', lambda: 'frozen-code')
    monkeypatch.setattr(experiment, 'env_file_stamp', lambda: {'frozen': True})
    def factory(cfg):
        index, sent = len(clients), []
        captured[index] = sent
        def handler(request):
            sent.append(json.loads(request.content))
            number = len(sent)
            call = [('read_evidence', {'packet_id': 'financial_excerpt'}),
                    ('read_evidence', {'packet_id': 'simulated_ledger'}),
                    ('calculate', {'operation': 'subtract', 'values': ['3745', '3301.5']}),
                    ('submit_report', {'summary': '已实现盈亏443.5元；有限固定证据研究。', 'open_questions': ['未研究真实当前价格']})][number - 1]
            return tool_response(*call, number)
        client = ActualModelClient(cfg, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client
    monkeypatch.setattr(experiment, 'ModelClient', factory)
    packets = experiment.evidence_packets(FIXTURE)
    records = []
    for variant in ('legacy_seed_rewrite', 'append_only'):
        record, saved = {'variant': variant}, []
        records.append(record)
        await experiment.run_variant(record, config=config(), system='stable role', initial={'task': experiment.TASK},
            packets=packets, store=None, deadline=experiment.time.monotonic() + 3600,
            frozen_source='frozen-code', frozen_environment={'frozen': True},
            save=lambda: saved.append(copy.deepcopy(record)))
        assert record['status'] == 'completed' and len(record['requests']) == 4
        assert record['three_useful_continuations_observed']
        assert record['report']['summary'].startswith('已实现盈亏443.5')
        assert record['rounds'][2]['results'][0]['result']['result'] == '443.5'
        assert record['content_review'] == 'pending'
        assert len({p['prompt_cache_key'] for p in captured[len(records)-1]}) == 1
        assert any(s.get('last_accepted_request_id') for s in saved)
    assert captured[0][0]['prompt_cache_key'] != captured[1][0]['prompt_cache_key']
    assert all(not r['prefix_diagnostic']['history_is_prefix'] for r in records[0]['requests'][1:])
    assert all(r['prefix_diagnostic']['history_is_prefix'] for r in records[1]['requests'][1:])
    assert 'comparison-opaque' in json.dumps(captured[0][-1])
    assert 'comparison-opaque' not in json.dumps(records)
    assert 'expected_financial_metrics' not in json.dumps(packets)


@pytest.mark.asyncio
@pytest.mark.parametrize('route', [None, 'direct'])
async def test_experiment_unknown_stream_failure_stops_before_other_arm(tmp_path, monkeypatch, route):
    monkeypatch.setattr(experiment, 'source_digest', lambda: 'frozen-code')
    monkeypatch.setattr(experiment, 'env_file_stamp', lambda: {'frozen': True})
    cfg = config(model_network_route='environment')
    class Store:
        def freeze_agent_team(self):
            return {'roles': {'filing_researcher': {'system_prompt': 'stable role'}}}
        def runtime_role_config(self, *args):
            return copy.deepcopy(cfg)
    selected_routes = []
    def isolated(*args, route=None):
        selected_routes.append(route)
        if route is not None:
            cfg['model_network_route'] = route
        return Store(), cfg
    monkeypatch.setattr(experiment, 'configure_isolated', isolated)
    sent = []
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created","response":{"id":"unfinished"}}\n\n'
            raise httpx.ReadError('controlled unknown generation')
    def factory(config):
        def handler(request):
            sent.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BrokenStream())
        return ActualModelClient(config, transport=httpx.MockTransport(handler))
    # Preserve the static summary API while replacing construction only.
    factory.usage_summary = ActualModelClient.usage_summary
    monkeypatch.setattr(experiment, 'ModelClient', factory)
    source = tmp_path / 'source.sqlite3'
    source.write_bytes(b'read-only-source-identity-fixture')
    args = SimpleNamespace(run_real=True, source=source, output=tmp_path/'comparison', reference_account='fixture',
                           role='filing_researcher', evidence=FIXTURE, seconds_per_session=10, route=route)
    assert await experiment.compare(args) == 2
    result = json.loads((args.output / 'summary.json').read_text())
    assert len(sent) == 1 and len(result['variants']) == 1
    assert result['remaining_variants_not_run'] == ['append_only']
    assert result['variants'][0]['requests'][0]['request_accepted'] is True
    assert result['variants'][0]['status'] == 'failed'
    assert result['all_model_usage']['failed_requests'] == 1
    assert selected_routes == [route]
    assert result['requested_configuration']['model_network_route'] == (route or 'environment')
    assert result['route_selection'] == (route or 'preserve_source')
    assert source.read_bytes() == b'read-only-source-identity-fixture'
