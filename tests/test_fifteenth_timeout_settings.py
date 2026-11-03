"""Real settings API preserves deliberate limits across partial saves/restarts."""
import httpx
import pytest

from app.config import LONG_TASK_TIMEOUT_DEFAULTS
from app.main import create_app
from tests.test_fifteenth_long_timeouts import PREVIOUS


@pytest.mark.asyncio
async def test_partial_timeout_edit_persists_effective_profile_and_survives_restart(tmp_path):
    database = tmp_path / 'partial-timeouts.sqlite3'
    app = create_app(database, mode='live', worker=False)
    app.state.store.set_meta('settings', {**PREVIOUS, 'quote_provider': 'manual'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as api:
        before = (await api.get('/api/settings')).json()
        assert {key: before[key] for key in LONG_TASK_TIMEOUT_DEFAULTS} == LONG_TASK_TIMEOUT_DEFAULTS
        response = await api.put('/api/settings', json={'model_stream_idle_timeout_seconds': 150})
        assert response.status_code == 200
        expected = {**LONG_TASK_TIMEOUT_DEFAULTS, 'model_stream_idle_timeout_seconds': 150}
        assert {key: response.json()[key] for key in expected} == expected
    restarted = create_app(database, mode='live', worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1') as api:
        after = (await api.get('/api/settings')).json()
        assert {key: after[key] for key in expected} == expected
        assert after['timeout_defaults_revision'] == 3


@pytest.mark.asyncio
async def test_explicit_old_limits_saved_through_api_are_not_migrated_again(tmp_path):
    database = tmp_path / 'explicit-timeouts.sqlite3'
    app = create_app(database, mode='live', worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as api:
        response = await api.put('/api/settings', json=PREVIOUS)
        assert response.status_code == 200
        assert {key: response.json()[key] for key in PREVIOUS} == PREVIOUS
        assert response.json()['timeout_defaults_revision'] == 3
    restarted = create_app(database, mode='live', worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1') as api:
        policy = (await api.get('/api/settings/timeout-policy')).json()
        assert policy['research']['request_hard_seconds'] == 300
        assert policy['synthesis']['request_hard_seconds'] == 360
        assert policy['child_budget_seconds'] == 600
        assert policy['analysis_budget_seconds'] == 1200


@pytest.mark.asyncio
async def test_saved_revision3_idle120_survives_unrelated_api_save_and_restart(tmp_path):
    database=tmp_path/'saved-idle120.sqlite3'
    app=create_app(database,mode='live',worker=False)
    saved={**LONG_TASK_TIMEOUT_DEFAULTS,'model_stream_idle_timeout_seconds':120,'timeout_defaults_revision':3,'quote_provider':'manual'}
    app.state.store.set_meta('settings',saved)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as api:
        before=(await api.get('/api/settings')).json()
        assert before['model_stream_idle_timeout_seconds']==120
        response=await api.put('/api/settings',json={'analysis_timeout_seconds':3500})
        assert response.status_code==200 and response.json()['model_stream_idle_timeout_seconds']==120
    restarted=create_app(database,mode='live',worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),base_url='http://127.0.0.1') as api:
        policy=(await api.get('/api/settings/timeout-policy')).json()
        assert policy['research']['stream_idle_seconds']==policy['synthesis']['stream_idle_seconds']==120
        assert policy['research']['request_hard_seconds']==policy['synthesis']['request_hard_seconds']==900
        assert policy['child_budget_seconds']==1800 and policy['analysis_budget_seconds']==3500
