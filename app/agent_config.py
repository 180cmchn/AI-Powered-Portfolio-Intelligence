"""Provider connections, discovered model catalogs and one global role directory.

All account matrices are joins over the global directory. Overrides can only
replace a provider/model pair and/or effort; immutable task snapshots contain
credential references, never their values. HTTP discovery never follows redirects.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, parse_qsl

import httpx

from .config import defaults, REASONING_EFFORTS, environment_connection_source
from .credentials import KeychainCredentials, CredentialError, fingerprint
from .domain import now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS model_providers(
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_connections(
 provider_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(provider_id,revision));
CREATE TABLE IF NOT EXISTS provider_catalogs(
 provider_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(provider_id,revision));
CREATE TABLE IF NOT EXISTS agent_definitions(
 role_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_agent_overrides(
 account_id TEXT NOT NULL, role_id TEXT NOT NULL, revision INTEGER NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(account_id,role_id));
'''

PROMPTS = Path(__file__).with_name('prompts')
RESEARCH_TOOL_NAMES = ['read', 'pdf_metadata', 'search_pdf', 'save_material', 'list_materials',
    'search_materials', 'get_material', 'read_material_range', 'list_recommendations',
    'get_recommendation', 'recall_context', 'history', 'sectors', 'sector_members',
    'sector_history', 'benchmark_history', 'fundamentals', 'calculate', 'query_market_rows', 'market_indicators']
MANAGER_TOOL_NAMES = RESEARCH_TOOL_NAMES + ['list_business_demands', 'list_forecast_targets', 'prepare_synthesis', 'list_agent_roles', 'spawn_research',
    'send_message', 'follow_up', 'wait_agents', 'agent_status', 'cancel_agent', 'finish_analysis']
CHILD_TOOL_NAMES = RESEARCH_TOOL_NAMES + ['report_progress', 'submit_report']
INITIAL_ROLES = [
    ('manager', '主 Agent', '根据本账户事实与目标自主分派、比较研究，唯一发布最终报告、建议与正式预测。'),
    ('filing_researcher', '公告与披露研究员', '搜集公告、报告与披露，区分事实、计划与评论，交付报告期、口径、来源及缺失项。'),
    ('market_researcher', '市场与价格研究员', '研究价格、成交、相对表现与趋势，交付可复算指标、局部推论、观察条件与反向证据。'),
    ('industry_researcher', '行业与政策研究员', '研究行业、政策和产业链，交付客观事件、影响机制、关联证券与不确定性。'),
    ('etf_researcher', 'ETF 与指数研究员', '研究基金及跟踪指数，交付跟踪标的、费用、披露持仓时点、流动性与资料缺口。'),
    ('portfolio_researcher', '组合与配置研究员', '分析本账户结构、集中度和配置情景，交付组合影响、风险敞口、计算与约束。'),
]
NINTH_PREVIOUS_PROMPTS = {
    'manager': '7744b22dd0ce824a2917b0de7a312b865cf73c1c1408e2ba77154dadc0f9fda8',
    'portfolio_researcher': '18cc7d58ab0bf324cbde0fda37139a7da001da3cc56adec0f50902ba12102039',
}

DISCOVERY_TTL_SECONDS = 3600
DISCOVERY_PAGE_LIMIT = 5
DISCOVERY_MODEL_LIMIT = 2000
DISCOVERY_RESPONSE_BYTES = 2_000_000
_DISCOVERY_TASKS: dict[tuple, asyncio.Task] = {}
_SAVE_TASKS: dict[tuple, asyncio.Task] = {}


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _loads(value, default=None):
    return json.loads(value) if value else default


def _digest(value):
    return hashlib.sha256(_dumps(value).encode()).hexdigest()


def normalize_api_base(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('Base URL 必须是有效的 HTTP(S) API 根地址')
    parsed = urlsplit(value.strip())
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('Base URL 不得含凭据、查询参数或片段')
    if parsed.scheme != 'https' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('远端供应商必须使用 HTTPS；HTTP 仅限本机')
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('Base URL 端口无效') from exc
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses'):
        if path.endswith(suffix):
            path = path[:-len(suffix)].rstrip('/')
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def _safe_next(base: str, value: str) -> str:
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError('模型目录分页链接无效')
    target = urljoin(base + '/models', value)
    a, b = urlsplit(base + '/models'), urlsplit(target)
    if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port) or b.username or b.password or b.fragment:
        raise ValueError('模型目录分页链接越过受信来源，已停止，未发送凭据')
    if any(k.lower() in {'key', 'api_key', 'apikey', 'token', 'access_token', 'authorization'} for k, _ in parse_qsl(b.query)):
        raise ValueError('模型目录分页链接包含不受支持的认证参数')
    if b.path.rstrip('/') != a.path.rstrip('/'):
        raise ValueError('模型目录分页链接离开 models 路径，已停止，未发送凭据')
    return target


def _catalog_page(payload: dict, base: str, current_url: str) -> tuple[list[dict], str | None]:
    if not isinstance(payload, dict) or payload.get('error') or payload.get('success') is False:
        raise ValueError('模型目录响应不是成功对象')
    if isinstance(payload.get('data'), dict):
        if payload.get('success') is not True:
            raise ValueError('模型目录封装没有明确 success=true')
        payload = payload['data']
        if payload.get('error') or payload.get('success') is False:
            raise ValueError('模型目录内层返回错误')
    if isinstance(payload.get('data'), list):
        rows, field = payload['data'], 'id'
    elif isinstance(payload.get('models'), list):
        rows, field = payload['models'], 'id_or_slug'
    else:
        raise ValueError('模型目录缺少 data[] 或 models[]；未视为成功空列表')
    models = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('模型目录条目不是对象')
        model_id = row.get('id') or (row.get('slug') if field == 'id_or_slug' else None)
        if not isinstance(model_id, str) or not model_id or len(model_id) > 512:
            raise ValueError('模型目录条目缺少有效原始 ID')
        # Preserve actual IDs. Deliberately do not infer capabilities from names.
        metadata = {k: row[k] for k in ('object', 'created', 'owned_by', 'type', 'context_length', 'capabilities')
                    if k in row and isinstance(row[k], (str, int, float, bool, list, dict, type(None)))}
        if len(_dumps(metadata)) > 12000:
            metadata = {'metadata_truncated': True}
        model_type = row.get('type')
        models.append({'id': model_id, 'metadata': metadata,
                       'suitability': 'non_dialogue' if model_type in {'embedding', 'embeddings', 'audio', 'speech', 'transcription'} else 'unknown',
                       'source_url': current_url})
    next_value = payload.get('next') or payload.get('next_page_url')
    if next_value is None and isinstance(payload.get('links'), dict):
        next_value = payload['links'].get('next')
    if next_value:
        next_url = _safe_next(base, next_value)
    elif payload.get('has_more') is True:
        cursor = payload.get('last_id') or (models[-1]['id'] if models else None)
        if not cursor:
            raise ValueError('目录声明还有分页但没有可用游标')
        query = dict(parse_qsl(urlsplit(current_url).query))
        query['after'] = cursor
        next_url = base + '/models?' + urlencode(query)
    else:
        next_url = None
    return models, next_url


class AgentConfigStore:
    def _vault(self):
        # Tests may explicitly inject an in-memory substitute. Production never
        # chooses it as a fallback when the platform store is unavailable.
        return getattr(self, '_credential_store', None) or KeychainCredentials()

    def migrate_agent_config(self, db, legacy_config=None):
        if db.execute("SELECT 1 FROM meta WHERE key='agent_config_migration'").fetchone():
            return
        config = {**defaults(), **(legacy_config or {})}
        if legacy_config is None:
            row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            config.update(_loads(row[0], {}) if row else {})
        provider_id = 'legacy-' + uuid.uuid4().hex[:12]
        key_env = config.get('model_key_env') or 'MODEL_API_KEY'
        secret = os.environ.get(key_env, '')
        reference = {'kind': 'environment', 'name': key_env, 'fingerprint': fingerprint(secret)} if secret else {'kind': 'missing'}
        connection_error = None
        try:
            base_url = normalize_api_base(config.get('model_base_url'))
        except ValueError:
            base_url = ''
            connection_error = '历史 Base URL 无效，请重新保存供应商连接；原始设置保留在升级备份中'
        if config.get('model_protocol') not in {'responses', 'chat_completions'}:
            connection_error = '历史协议无效，请明确选择 Responses 或 Chat Completions'
        provider = {'id': provider_id, 'display_name': '供应商 1（迁移）', 'base_url': base_url,
                    'protocol': config['model_protocol'], 'revision': 1, 'credential_revision': 1,
                    'credential_reference': reference, 'created_at': now(), 'updated_at': now(), 'archived': False,
                    'connection_error': connection_error}
        db.execute('INSERT INTO model_providers VALUES(?,?,?)', (provider_id, 1, _dumps(provider)))
        db.execute('INSERT INTO provider_connections VALUES(?,?,?)', (provider_id, 1, _dumps(provider)))
        binding = {'provider_id': provider_id, 'model_id': config.get('model_name') or '',
                   'reasoning_effort': config.get('reasoning_effort', 'medium'), 'validation': 'legacy_unverified'}
        common = (PROMPTS / 'researcher.txt').read_text()
        for role_id, name, duty in INITIAL_ROLES:
            prompt = (PROMPTS / ('manager.txt' if role_id == 'manager' else role_id + '.txt')).read_text()
            role = {'role_id': role_id, 'name': name, 'responsibility': duty,
                    'system_prompt': prompt if role_id == 'manager' else prompt + '\n\n' + common,
                    'tools': MANAGER_TOOL_NAMES if role_id == 'manager' else CHILD_TOOL_NAMES,
                    'enabled': True, 'version': 1, 'prompt_version': 1, 'tool_version': 1,
                    'default_binding': copy.deepcopy(binding), 'updated_at': now(), 'custom': False}
            db.execute('INSERT INTO agent_definitions VALUES(?,?,?)', (role_id, 1, _dumps(role)))
        db.execute("INSERT OR REPLACE INTO meta VALUES('agent_catalog_version','1')")
        db.execute("INSERT OR REPLACE INTO meta VALUES('agent_override_revision','0')")
        db.execute("INSERT INTO meta VALUES('agent_config_migration',?)", (_dumps({
            'at': now(), 'legacy_provider_id': provider_id, 'credentials': 'legacy environment reference only',
            'legacy_binding': 'preserved_unverified', 'private_role_conflicts': [],
            'note': '旧模型名称只作为角色历史绑定保留；尚未自动发现。没有已有 Profile 或账户私有角色结构。'}),))

    def migrate_eighth_roles(self, db):
        if db.execute("SELECT 1 FROM meta WHERE key='eighth_role_migration'").fetchone():
            return
        previous_prompts = {'manager': '89fc4c1e5f3c4f0064e512ed27899df5c8e150463790d8aa9b951448e46e9c66', 'filing_researcher': '68411ea30363f2d2b964114243cff515c6f5eb38d2fb3b4820371317a4b4da71', 'market_researcher': '4a9eb21b273193087d26bace410d5b0a6448987254707f4b5e1d10eb2b5af431', 'industry_researcher': 'fdbf1c132e6cd996c7980217dab01d79fc3cd6f99632064ec090bc9e0789343e', 'etf_researcher': '781844debc5da7be1c5ccfa10f26a35b2d6158dba20f400f2c5fdc10e2a4f855', 'portfolio_researcher': 'e0fb39dd7c0b3062d4fbfc2cac391ee03a7756b442e0c689fae547bd965ca5cb'}
        changed = []
        for row in db.execute('SELECT role_id,body FROM agent_definitions').fetchall():
            role = _loads(row['body'])
            updated = False
            expected = MANAGER_TOOL_NAMES if row['role_id'] == 'manager' else CHILD_TOOL_NAMES
            old_tools = [t for t in expected if t not in ('query_market_rows', 'market_indicators')]
            if role.get('tools') == old_tools and not role.get('custom'):
                role['tools'] = expected
                role['tool_version'] = role.get('tool_version', 1) + 1
                updated = True
            previous = previous_prompts.get(row['role_id'])
            if previous and hashlib.sha256(role.get('system_prompt', '').encode()).hexdigest() == previous:
                prompt = (PROMPTS / (row['role_id'] + '.txt')).read_text()
                if row['role_id'] != 'manager':
                    prompt += '\n\n' + (PROMPTS / 'researcher.txt').read_text()
                if prompt != role['system_prompt']:
                    role['system_prompt'] = prompt
                    role['prompt_version'] = role.get('prompt_version', 1) + 1
                    updated = True
            if updated:
                role.update(version=role['version']+1, updated_at=now())
                db.execute('UPDATE agent_definitions SET version=?,body=? WHERE role_id=?',
                           (role['version'], _dumps(role), row['role_id']))
                changed.append(row['role_id'])
        if changed:
            version = db.execute("SELECT value FROM meta WHERE key='agent_catalog_version'").fetchone()
            db.execute("INSERT OR REPLACE INTO meta VALUES('agent_catalog_version',?)", (str(int(version[0])+1),))
        db.execute("INSERT INTO meta VALUES('eighth_role_migration',?)", (_dumps({'at': now(), 'updated_roles': changed,
            'note': 'Only exact previous built-in prompts/tools upgraded; custom instructions and frozen teams retained.'}),))

    def _provider(self, provider_id, db):
        row = db.execute('SELECT body FROM model_providers WHERE id=?', (provider_id,)).fetchone()
        if not row:
            raise KeyError('供应商不存在')
        return _loads(row[0])

    def migrate_ninth_roles(self, db):
        if db.execute("SELECT 1 FROM meta WHERE key='ninth_role_migration'").fetchone():
            return
        changed = []
        for role_id, expected_hash in NINTH_PREVIOUS_PROMPTS.items():
            row = db.execute('SELECT body FROM agent_definitions WHERE role_id=?', (role_id,)).fetchone()
            if not row:
                continue
            role = _loads(row[0])
            if role.get('custom') or hashlib.sha256(role.get('system_prompt', '').encode()).hexdigest() != expected_hash:
                continue
            prompt = (PROMPTS / (role_id + '.txt')).read_text()
            if role_id != 'manager':
                prompt += '\n\n' + (PROMPTS / 'researcher.txt').read_text()
            if prompt == role['system_prompt']:
                continue
            role.update(system_prompt=prompt, version=role['version'] + 1,
                        prompt_version=role['prompt_version'] + 1, updated_at=now())
            db.execute('UPDATE agent_definitions SET version=?,body=? WHERE role_id=?', (role['version'], _dumps(role), role_id))
            changed.append(role_id)
        if changed:
            version = db.execute("SELECT value FROM meta WHERE key='agent_catalog_version'").fetchone()
            db.execute("INSERT OR REPLACE INTO meta VALUES('agent_catalog_version',?)", (str(int(version[0]) + 1),))
        db.execute("INSERT INTO meta VALUES('ninth_role_migration',?)", (_dumps({'at': now(), 'updated_roles': changed,
            'note': 'Only exact eighth built-in prompts upgraded; custom roles, bindings and frozen task authors retained.'}),))

    def _provider_catalog(self, provider_id, revision, db):
        row = db.execute('SELECT body FROM provider_catalogs WHERE provider_id=? AND revision=?', (provider_id, revision)).fetchone()
        return _loads(row[0]) if row else {'status': 'not_fetched', 'models': [], 'revision': revision,
                                         'complete': False, 'selectable': False}

    def migrate_tenth_roles(self, db):
        if db.execute("SELECT 1 FROM meta WHERE key='tenth_role_migration'").fetchone():
            self.migrate_tenth_delivery_prompt(db)
            return
        changed = []
        row = db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()
        if row:
            role = _loads(row[0])
            prior = 'd68e0d4263bfa36af2359ec2e0011bdbbc7c92c24860d7acc142c074c071ea99'
            if not role.get('custom') and hashlib.sha256(role.get('system_prompt', '').encode()).hexdigest() == prior:
                role.update(system_prompt=(PROMPTS / 'manager.txt').read_text(), version=role['version'] + 1,
                            prompt_version=role['prompt_version'] + 1, updated_at=now())
                db.execute("UPDATE agent_definitions SET version=?,body=? WHERE role_id='manager'", (role['version'], _dumps(role)))
                version = db.execute("SELECT value FROM meta WHERE key='agent_catalog_version'").fetchone()
                db.execute("INSERT OR REPLACE INTO meta VALUES('agent_catalog_version',?)", (str(int(version[0]) + 1),))
                changed.append('manager')
        db.execute("INSERT INTO meta VALUES('tenth_role_migration',?)", (_dumps({'at': now(), 'updated_roles': changed,
            'note': 'Only exact ninth built-in manager prompt upgraded; custom prompts, bindings, effort and frozen teams retained.'}),))
        self.migrate_tenth_delivery_prompt(db)

    def migrate_tenth_delivery_prompt(self, db):
        if db.execute("SELECT 1 FROM meta WHERE key='tenth_delivery_prompt_migration'").fetchone():
            return
        row = db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()
        changed = []
        if row:
            role = _loads(row[0])
            previous = 'a53144260c6dec930ba3190da434ebd03762b7b0ecc772850966f485fa8259cb'
            if not role.get('custom') and hashlib.sha256(role.get('system_prompt', '').encode()).hexdigest() == previous:
                role.update(system_prompt=(PROMPTS / 'manager.txt').read_text(), version=role['version'] + 1,
                            prompt_version=role['prompt_version'] + 1, updated_at=now())
                db.execute("UPDATE agent_definitions SET version=?,body=? WHERE role_id='manager'", (role['version'], _dumps(role)))
                version = db.execute("SELECT value FROM meta WHERE key='agent_catalog_version'").fetchone()
                db.execute("INSERT OR REPLACE INTO meta VALUES('agent_catalog_version',?)", (str(int(version[0]) + 1),))
                changed.append('manager')
        db.execute("INSERT INTO meta VALUES('tenth_delivery_prompt_migration',?)", (_dumps({'at': now(), 'updated_roles': changed,
            'note': 'Exact initial tenth built-in only; more concise delivery, no change to binding, effort or request deadlines.'}),))

    def _public_provider(self, provider, db):
        body = {k: v for k, v in provider.items() if k != 'credential_reference'}
        ref = provider.get('credential_reference') or {}
        body['key_configured'] = ref.get('kind') not in {None, 'missing'}
        body['key_mask'] = '••••••••' if body['key_configured'] else ''
        body['credential_id'] = ref.get('fingerprint', '')[:12]
        body['connection_source'] = provider.get('connection_source') or ('environment' if ref.get('kind') == 'environment' else 'saved')
        body['key_state'] = 'reference_saved' if body['key_configured'] else 'missing'
        if ref.get('kind') == 'environment':
            current = os.environ.get(ref.get('name', ''), '')
            body['key_state'] = 'available' if current and fingerprint(current) == ref.get('fingerprint') else 'environment_changed' if current else 'missing'
        body['key_available'] = body['key_state'] == 'available' if ref.get('kind') == 'environment' else None
        catalog = self._provider_catalog(provider['id'], provider['revision'], db)
        catalog = copy.deepcopy(catalog)
        if catalog.get('fetched_at'):
            catalog['cache_expired'] = (datetime.fromisoformat(now()) - datetime.fromisoformat(catalog['fetched_at'])).total_seconds() > DISCOVERY_TTL_SECONDS
        else:
            catalog['cache_expired'] = False
        body['catalog'] = catalog
        return body

    def provider(self, provider_id):
        with self.connect() as db:
            return self._public_provider(self._provider(provider_id, db), db)

    def list_providers(self, include_archived=True):
        with self.connect() as db:
            rows = [_loads(r[0]) for r in db.execute('SELECT body FROM model_providers ORDER BY rowid')]
            return [self._public_provider(p, db) for p in rows if include_archived or not p.get('archived')]

    def environment_connection(self):
        """Read-only reconciliation; a one-time migration never silently wins.

        Preview is an opaque configuration revision, not a credential digest.
        It is stored only in memory and expires whenever the proposed inputs or
        saved provider revision change. Import requires this exact preview.
        """
        config = defaults()
        key_name = config.get('model_key_env') or 'MODEL_API_KEY'
        secret = os.getenv(key_name, '')
        error = None
        try:
            base = normalize_api_base(config['model_base_url'])
        except ValueError:
            base, error = '', '环境 Base URL 无效，请修正后安全重启'
        protocol = config['model_protocol']
        if protocol not in {'responses', 'chat_completions'}:
            error = '环境协议无效，请使用 responses 或 chat_completions'
        source = environment_connection_source()
        with self.connect() as db:
            rows = [_loads(r[0]) for r in db.execute('SELECT body FROM model_providers ORDER BY rowid')]
            migration = db.execute("SELECT value FROM meta WHERE key='agent_config_migration'").fetchone()
            legacy_id = (_loads(migration[0], {}) if migration else {}).get('legacy_provider_id')
            # Reuse the migration connection, or a previously explicitly imported
            # connection. Never overwrite a provider edited through the web UI.
            target = next((p for p in rows if p.get('connection_source') == 'environment' and p.get('environment_key_name') == key_name), None)
            legacy = next((p for p in rows if p['id'] == legacy_id), None)
            if not target and legacy and legacy.get('connection_source') != 'saved' and legacy['revision'] == 1 and legacy.get('credential_reference', {}).get('kind') in {'missing', 'environment'}:
                target = legacy
            revoked_ids = {p['id'] for p in rows if db.execute('SELECT 1 FROM meta WHERE key=?', ('revoked_provider_credentials:' + p['id'],)).fetchone()}
            # A revoked migration/import cannot be resurrected by re-reading env.
            revoked = bool((target and target['id'] in revoked_ids) or (not target and legacy_id in revoked_ids))
            if revoked:
                error = '此环境连接已明确撤销；环境导入不会恢复它。请在供应商表单明确录入新凭据。'
            exact = next((p for p in rows if not p.get('archived') and p['base_url'] == base and p['protocol'] == protocol
                          and p.get('credential_reference', {}).get('fingerprint') == fingerprint(secret) and secret), None)
            if exact and exact['id'] not in revoked_ids:
                target = exact
            same = bool(exact and target and exact['id'] == target['id'])
            revision = target['revision'] if target else None
            identity = (base, protocol, key_name, fingerprint(secret), target['id'] if target else None, revision, revoked, source['restart_required'])
            previous = getattr(self, '_environment_preview', None)
            if not previous or previous[0] != identity:
                self._environment_preview = (identity, uuid.uuid4().hex)
            if source['restart_required']:
                error = error or '.env 与当前进程不同；请先按手册安全重启，再检测和导入'
            return {'base_url': base, 'protocol': protocol, 'key_present': bool(secret), 'key_name': key_name,
                    'source': source, 'provider_id': target['id'] if target else None, 'provider_revision': revision,
                    'status': 'revoked' if revoked else 'invalid' if error else 'missing_key' if not secret else 'current' if same else 'pending_import',
                    'can_import': bool(secret and not error), 'changed': not same,
                    'preview_id': self._environment_preview[1], 'error': error,
                    'action': '供应商已识别；请从实际目录为主 Agent 选择模型' if same else '确认此目标后显式导入环境连接；已有网页连接与角色绑定保持原值',
                    'existing_saved_connections': len(rows), 'migration_completed': bool(migration)}

    async def import_environment_connection(self, preview_id, transport=None):
        preview = self.environment_connection()
        if preview_id != preview['preview_id']:
            raise ValueError('环境或已保存连接已变化，请重新检测并确认目标')
        if not preview['can_import']:
            raise ValueError(preview['error'] or '环境中没有可用 API Key')
        key_name = preview['key_name']
        reference = {'kind': 'environment', 'name': key_name, 'fingerprint': fingerprint(os.getenv(key_name, ''))}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._provider(preview['provider_id'], db) if preview['provider_id'] else None
            if old and old['revision'] != preview['provider_revision']:
                raise ValueError('供应商已修改，请重新检测')
            if old and db.execute('SELECT 1 FROM meta WHERE key=?', ('revoked_provider_credentials:' + old['id'],)).fetchone():
                raise ValueError('已撤销连接不能通过环境导入恢复')
            if old and preview['status'] == 'current':
                provider = old
            else:
                count = db.execute('SELECT COUNT(*) FROM model_providers').fetchone()[0]
                provider = {**(old or {}), 'id': old['id'] if old else uuid.uuid4().hex,
                    'display_name': old['display_name'] if old else f'供应商 {count + 1}（环境）',
                    'base_url': preview['base_url'], 'protocol': preview['protocol'],
                    'revision': old['revision'] + 1 if old else 1,
                    'credential_revision': old['credential_revision'] + 1 if old else 1,
                    'credential_reference': reference, 'connection_source': 'environment', 'environment_key_name': key_name,
                    'created_at': old['created_at'] if old else now(), 'updated_at': now(), 'archived': False, 'connection_error': None}
                db.execute('INSERT OR REPLACE INTO model_providers VALUES(?,?,?)', (provider['id'], provider['revision'], _dumps(provider)))
                db.execute('INSERT INTO provider_connections VALUES(?,?,?)', (provider['id'], provider['revision'], _dumps(provider)))
        await self.discover_models(provider['id'], transport=transport)
        return self.provider(provider['id'])

    def configuration_readiness(self, account_id=None):
        roles = self.list_agent_roles(account_id)
        manager = next((r for r in roles if r['role_id'] == 'manager'), None)
        providers = {p['id']: p for p in self.list_providers()}
        resolved = (manager or {}).get('resolved', {})
        provider = providers.get(resolved.get('provider_id'))
        error = (manager or {}).get('config_error')
        if provider and provider['key_state'] == 'environment_changed':
            error = '环境凭据已变化；请检测并显式更新环境连接'
        configured = bool(manager and resolved.get('model_id') and not error)
        any_connection = any(p['key_configured'] and not p.get('archived') for p in providers.values())
        next_step = ('供应商已识别，主 Agent 待绑定模型；请从实际目录选择模型' if any_connection and not resolved.get('model_id')
                     else error or ('模型已绑定；函数和原生搜索能力以明确探测或实际请求为准' if configured else '请添加或导入供应商连接'))
        return {'account_id': account_id, 'local_api': 'ready', 'provider_saved': any(not p.get('archived') for p in providers.values()),
                'manager_provider_saved': bool(provider),
                'key_state': provider['key_state'] if provider else 'missing',
                'catalog_status': provider['catalog']['status'] if provider else 'not_fetched',
                'manager_binding': resolved, 'manager_ready': configured, 'manager_error': error,
                'next_step': next_step,
                'roles': [{'role_id': r['role_id'], 'binding_source': r['resolved']['binding_source'], 'error': r.get('config_error')} for r in roles],
                'functions': 'unverified', 'native_search': 'protocol_unsupported' if provider and provider['protocol'] == 'chat_completions' else 'unverified'}

    def provider_operation(self, operation_id):
        if not re.fullmatch(r'[a-f0-9-]{32,36}', operation_id):
            raise ValueError('保存操作标识无效')
        with self.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', ('provider_operation:' + operation_id,)).fetchone()
            value = _loads(row[0]) if row else None
            return {'status': 'committed', 'committed_revision': value.get('revision'),
                    'provider': self._public_provider(self._provider(value['provider_id'], db), db)} if value else {'status': 'not_found'}

    async def save_provider(self, payload: dict, provider_id=None, transport=None, operation_id=None):
        if operation_id:
            if not re.fullmatch(r'[a-f0-9-]{32,36}', operation_id):
                raise ValueError('保存操作标识无效')
            # Persist only non-sensitive intent. Reusing an operation identifier
            # returns its first committed connection and never rotates its key.
            identity = _digest({'provider_id': provider_id, 'payload': {k: v for k, v in payload.items() if k != 'api_key'}})
            with self.connect() as db:
                row = db.execute('SELECT value FROM meta WHERE key=?', ('provider_operation:' + operation_id,)).fetchone()
                if row:
                    saved = _loads(row[0])
                    if saved['identity'] != identity:
                        raise ValueError('操作标识已用于其他输入，请先核对上次保存')
                    return self._public_provider(self._provider(saved['provider_id'], db), db)
            key = (str(self.path.resolve()), operation_id, id(asyncio.get_running_loop()))
            if key in _SAVE_TASKS:
                active_identity, task = _SAVE_TASKS[key]
                if active_identity != identity:
                    raise ValueError('操作标识正在保存其他输入')
            else:
                task = asyncio.create_task(self._save_provider(payload, provider_id, transport, operation_id, identity))
                _SAVE_TASKS[key] = (identity, task)
                task.add_done_callback(lambda done: _SAVE_TASKS.pop(key, None))
            return await asyncio.shield(task)
        return await self._save_provider(payload, provider_id, transport)

    async def _save_provider(self, payload: dict, provider_id=None, transport=None, operation_id=None, identity=None):
        if not isinstance(payload, dict) or set(payload) - {'api_key', 'base_url', 'protocol'}:
            raise ValueError('供应商只接受 api_key、base_url、protocol 三项连接输入')
        with self.connect() as db:
            old = self._provider(provider_id, db) if provider_id else None
        base = normalize_api_base(payload.get('base_url', old['base_url'] if old else ''))
        protocol = payload.get('protocol', old['protocol'] if old else '')
        if not isinstance(protocol, str) or protocol not in {'responses', 'chat_completions'}:
            raise ValueError('协议仅支持 responses 或 chat_completions')
        secret = payload.get('api_key')
        if secret is not None and (not isinstance(secret, str) or len(secret) > 16384 or '\n' in secret or '\r' in secret):
            raise ValueError('API Key 格式无效')
        if secret and ('••••' in secret or secret.strip('*') == ''):
            raise ValueError('掩码不能作为新 API Key 保存')
        if not old and not secret:
            raise ValueError('新供应商需要输入 API Key')
        ref = await asyncio.to_thread(self._vault().put, secret) if secret else copy.deepcopy(old['credential_reference'])
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # Detect concurrent edits before publishing a new credential revision.
                if old and self._provider(provider_id, db)['revision'] != old['revision']:
                    raise ValueError('供应商已被其他操作修改，请刷新后重试')
                provider_id = provider_id or uuid.uuid4().hex
                count = db.execute('SELECT COUNT(*) FROM model_providers').fetchone()[0]
                changed = not old or bool(secret) or old['base_url'] != base or old['protocol'] != protocol
                revision = (old['revision'] + int(changed)) if old else 1
                provider = {'id': provider_id, 'display_name': old['display_name'] if old else f'供应商 {count + 1}',
                            'base_url': base, 'protocol': protocol, 'revision': revision,
                            'credential_revision': (old['credential_revision'] + int(bool(secret))) if old else 1,
                            'credential_reference': ref, 'created_at': old['created_at'] if old else now(),
                            'updated_at': now(), 'archived': old.get('archived', False) if old else False,
                            'connection_source': 'saved'}
                db.execute('INSERT OR REPLACE INTO model_providers VALUES(?,?,?)', (provider_id, revision, _dumps(provider)))
                db.execute('INSERT OR REPLACE INTO provider_connections VALUES(?,?,?)', (provider_id, revision, _dumps(provider)))
                if operation_id:
                    db.execute('INSERT INTO meta VALUES(?,?)', ('provider_operation:' + operation_id,
                        _dumps({'provider_id': provider_id, 'revision': revision, 'identity': identity, 'at': now()})))
        except BaseException:
            if secret:
                await asyncio.to_thread(self._vault().delete, ref)
            raise
        await self.discover_models(provider_id, transport=transport)
        return self.provider(provider_id)

    def archive_provider(self, provider_id, archived=True):
        with self.connect() as db:
            provider = self._provider(provider_id, db)
            provider.update(archived=bool(archived), updated_at=now())
            db.execute('UPDATE model_providers SET body=? WHERE id=?', (_dumps(provider), provider_id))
            references = sum(_loads(r[0]).get('default_binding', {}).get('provider_id') == provider_id
                             for r in db.execute('SELECT body FROM agent_definitions'))
        return {**self.provider(provider_id), 'referenced_by_global_roles': references,
                'note': '连接已归档，新任务明确显示不可用；历史快照及已发布预测保留。' if archived else '连接已恢复'}

    async def clear_provider_credential(self, provider_id):
        with self.connect() as db:
            provider = self._provider(provider_id, db)
            refs = [_loads(r[0]).get('credential_reference') for r in db.execute(
                'SELECT body FROM provider_connections WHERE provider_id=?', (provider_id,))]
        for reference in refs:
            if reference:
                await asyncio.to_thread(self._vault().delete, reference)
        with self.connect() as db:
            provider = self._provider(provider_id, db)
            provider.update(revision=provider['revision'] + 1, credential_revision=provider['credential_revision'] + 1,
                            credential_reference={'kind': 'missing'}, updated_at=now())
            db.execute('UPDATE model_providers SET revision=?,body=? WHERE id=?', (provider['revision'], _dumps(provider), provider_id))
            db.execute('INSERT INTO provider_connections VALUES(?,?,?)', (provider_id, provider['revision'], _dumps(provider)))
            # Environment references cannot be erased from the user's process.
            # A revocation ledger prevents their old snapshots being used again.
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('revoked_provider_credentials:' + provider_id,
                _dumps({'through_revision': provider['credential_revision'] - 1, 'at': now()})))
        return self.provider(provider_id)

    async def discover_models(self, provider_id, force=False, continue_page=False, transport=None):
        with self.connect() as db:
            provider = self._provider(provider_id, db)
            old = self._provider_catalog(provider_id, provider['revision'], db)
        if provider.get('archived'):
            return {**old, 'error': '供应商已归档'}
        if not force and not continue_page and old.get('fetched_at') and old.get('status') in {'success', 'empty', 'partial'}:
            if (datetime.fromisoformat(now()) - datetime.fromisoformat(old['fetched_at'])).total_seconds() < DISCOVERY_TTL_SECONDS:
                return old
        key = (str(self.path.resolve()), provider_id, provider['revision'], id(asyncio.get_running_loop()))
        task = _DISCOVERY_TASKS.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._discover(provider, old, continue_page, transport))
            _DISCOVERY_TASKS[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and _DISCOVERY_TASKS.get(key) is task:
                _DISCOVERY_TASKS.pop(key, None)

    async def _discover(self, provider, old, continuation, transport):
        provider_id, revision, base = provider['id'], provider['revision'], provider['base_url']
        models = list(old.get('models', [])) if continuation and old.get('next_url') else []
        next_url = old.get('next_url') if continuation else None
        next_url = _safe_next(base, next_url) if next_url else base + '/models'
        failure_cache = copy.deepcopy(old)
        state = {**old, 'status': 'fetching', 'started_at': now(), 'revision': revision}
        self._save_catalog_if_current(provider, state)
        try:
            secret = await asyncio.to_thread(self._vault().get, provider.get('credential_reference'))
            visited = set()
            async with asyncio.timeout(45), httpx.AsyncClient(headers={'Authorization': 'Bearer ' + secret, 'Accept': 'application/json'},
                    follow_redirects=False, timeout=15, transport=transport) as client:
                for _ in range(DISCOVERY_PAGE_LIMIT):
                    if next_url in visited:
                        raise ValueError('模型目录分页循环，已停止')
                    visited.add(next_url)
                    async with client.stream('GET', next_url) as response:
                        status = response.status_code
                        if status != 200:
                            kind = ('authentication_failed' if status in {401, 403} else 'rate_limited' if status == 429
                                    else 'unsupported' if status in {404, 405} else 'redirect_blocked' if 300 <= status < 400 else 'http_error')
                            return self._discovery_failure(provider, failure_cache, kind, f'模型目录 HTTP {status}；未尝试其他主机或凭据', status)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > DISCOVERY_RESPONSE_BYTES:
                                raise ValueError('模型目录响应超过大小上限')
                    page, following = _catalog_page(json.loads(content), base, next_url)
                    if any(secret and (secret == model['id'] or (len(secret) >= 8 and secret in model['id'])) for model in page):
                        raise ValueError('模型目录将凭据反射为模型标识，已拒绝')
                    if following and len(secret) >= 8 and secret in following:
                        raise ValueError('分页链接反射凭据，已拒绝')
                    for model in page:
                        if len(secret) >= 8 and secret and secret in _dumps(model.get('metadata', {})):
                            model['metadata'] = {'redacted': True}
                    existing = {m['id'] for m in models}
                    for model in page:
                        if model['id'] not in existing:
                            model['discovered_at'] = now()
                            models.append(model)
                            existing.add(model['id'])
                    next_url = following
                    retained = {m['id']: m for m in old.get('models', [])}
                    retained.update({m['id']: m for m in models})
                    failure_cache = {**old, 'models': list(retained.values()), 'complete': False,
                        'fetched_at': old.get('fetched_at') or now(), 'next_url': following,
                        'partial_refresh_at': now(), 'count': len(retained), 'selectable': True}
                    if not next_url or len(models) >= DISCOVERY_MODEL_LIMIT:
                        break
            state = {'status': 'partial' if next_url else 'success' if models else 'empty', 'models': models,
                     'revision': revision, 'credential_revision': provider['credential_revision'],
                     'base_url': base, 'protocol': provider['protocol'], 'fetched_at': now(),
                     'attempted_at': now(), 'complete': not bool(next_url), 'next_url': next_url,
                     'selectable': True, 'source': 'provider_http_api', 'count': len(models), 'error': None,
                     'note': '已列出模型不代表请求额度、联网或 effort 已验证。'}
            if not self._save_catalog_if_current(provider, state):
                return {'status': 'superseded', 'revision': revision, 'error': '连接已修改，迟到目录未写入当前配置'}
            return state
        except (httpx.TimeoutException, TimeoutError):
            return self._discovery_failure(provider, failure_cache, 'timeout', '模型目录请求超时')
        except httpx.RequestError:
            return self._discovery_failure(provider, failure_cache, 'network_error', '模型目录网络连接失败')
        except CredentialError as exc:
            return self._discovery_failure(provider, failure_cache, 'credential_unavailable', str(exc))
        except (ValueError, TypeError, KeyError):
            # Never reflect provider response bodies, headers, URLs containing
            # tokens or attacker-controlled model metadata into error messages.
            return self._discovery_failure(provider, failure_cache, 'invalid_format', '模型目录结构、分页或 JSON 无效；未视为成功空列表')

    def _discovery_failure(self, provider, old, status, error, http_status=None):
        state = {**old, 'status': status, 'error': error, 'http_status': http_status, 'attempted_at': now(),
                 'cache_retained': bool(old.get('fetched_at')), 'selectable': bool(old.get('fetched_at')),
                 'revision': provider['revision']}
        if not self._save_catalog_if_current(provider, state):
            return {'status': 'superseded', 'revision': provider['revision'], 'error': '连接已修改，迟到结果未写入'}
        return state

    def _save_catalog_if_current(self, provider, state):
        with self.connect() as db:
            current = self._provider(provider['id'], db)
            if current['revision'] != provider['revision']:
                return False
            db.execute('INSERT OR REPLACE INTO provider_catalogs VALUES(?,?,?)',
                       (provider['id'], provider['revision'], _dumps(state)))
        return True

    def _validate_binding(self, payload, db, allow_effort_only=False):
        if not isinstance(payload, dict):
            raise ValueError('模型绑定必须是对象')
        if set(payload) - {'provider_id', 'model_id', 'reasoning_effort'}:
            raise ValueError('模型绑定只接受 provider_id、model_id、reasoning_effort；不能修改角色职责或启停')
        if ('provider_id' in payload) != ('model_id' in payload):
            raise ValueError('provider_id 与 model_id 必须成对设置')
        if 'reasoning_effort' in payload and payload['reasoning_effort'] not in REASONING_EFFORTS:
            raise ValueError('reasoning_effort 只接受 low、medium、high、xhigh、max')
        if not payload:
            raise ValueError('覆盖不能为空；恢复默认请明确清除覆盖')
        result = copy.deepcopy(payload)
        if 'provider_id' in payload:
            if any(not isinstance(payload[k], str) or not payload[k] or len(payload[k]) > 512 for k in ('provider_id', 'model_id')):
                raise ValueError('供应商与模型 ID 必须是非空字符串')
            provider = self._provider(payload['provider_id'], db)
            if provider.get('archived'):
                raise ValueError('供应商已归档，不能新建绑定')
            catalog = self._provider_catalog(provider['id'], provider['revision'], db)
            if not catalog.get('selectable') or payload['model_id'] not in {m['id'] for m in catalog.get('models', [])}:
                raise ValueError('只能选择该供应商当前连接修订实际发现的模型；请获取或刷新模型列表')
            result['validation'] = 'discovered'
            result['discovered_revision'] = provider['revision']
        elif not allow_effort_only:
            raise ValueError('请同时选择供应商及已发现的模型')
        return result

    def list_agent_roles(self, account_id=None, include_archived=False):
        with self.connect() as db:
            if account_id:
                self.account(account_id, db)
            return self._roles(account_id, db, include_archived)

    def _roles(self, account_id, db, include_archived=False):
        rows = [_loads(r[0]) for r in db.execute("SELECT body FROM agent_definitions ORDER BY CASE role_id WHEN 'manager' THEN 0 ELSE 1 END,rowid")]
        overrides = {r['role_id']: _loads(r['body']) for r in db.execute(
            'SELECT role_id,body FROM account_agent_overrides WHERE account_id=?', (account_id or '',))}
        result = []
        for definition in rows:
            if not include_archived and not definition['enabled']:
                continue
            override = overrides.get(definition['role_id'], {})
            default = definition['default_binding']
            source = override if override.get('provider_id') is not None else default
            resolved = {k: source.get(k) for k in ('provider_id', 'model_id', 'validation', 'discovered_revision')}
            resolved.update(reasoning_effort=override.get('reasoning_effort', default.get('reasoning_effort', 'medium')),
                            binding_source='account_override' if 'provider_id' in override else 'global_default',
                            effort_source='account_override' if 'reasoning_effort' in override else 'global_default')
            error, warning = None, None
            try:
                provider = self._provider(resolved.get('provider_id'), db)
                catalog = self._provider_catalog(provider['id'], provider['revision'], db)
                if provider.get('connection_error'):
                    error = provider['connection_error']
                elif provider.get('archived'):
                    error = '供应商已归档'
                elif provider.get('credential_reference', {}).get('kind') == 'missing':
                    error = '供应商未配置 API Key'
                elif provider.get('credential_reference', {}).get('kind') == 'environment' and fingerprint(os.getenv(provider['credential_reference'].get('name', ''), '')) != provider['credential_reference'].get('fingerprint'):
                    error = '环境凭据已变化或缺失，请检测并显式更新环境连接'
                elif not resolved.get('model_id'):
                    error = '尚未选择模型'
                elif resolved.get('validation') == 'legacy_unverified':
                    if resolved['model_id'] in {m['id'] for m in catalog.get('models', [])} and catalog.get('selectable'):
                        resolved['validation'] = 'discovered'
                        resolved['discovered_revision'] = provider['revision']
                    else:
                        error = '迁移历史模型未在实际目录确认；请获取目录并选择模型'
                elif resolved.get('discovered_revision') != provider['revision']:
                    if resolved['model_id'] in {m['id'] for m in catalog.get('models', [])} and catalog.get('selectable'):
                        resolved['validation'] = 'discovered'
                    else:
                        error = '供应商连接已改变，绑定模型尚未在新连接中确认'
                elif catalog.get('complete') and catalog.get('fetched_at') and resolved['model_id'] not in {m['id'] for m in catalog.get('models', [])}:
                    error = '最新完整列表未列出此模型；保留原绑定，需确认'
                elif catalog.get('status') not in {'success', 'empty', 'partial'}:
                    warning = '模型目录刷新失败，保留此前同连接的发现记录'
                resolved['provider_revision'] = provider['revision']
                resolved['credential_revision'] = provider['credential_revision']
                resolved['provider_display_name'] = provider['display_name']
                resolved['protocol'] = provider['protocol']
            except KeyError:
                error = '供应商不存在'
            if resolved.get('reasoning_effort') not in REASONING_EFFORTS:
                error = 'reasoning_effort 必须为 low、medium、high、xhigh、max；迁移保留了无效历史值，请明确修正'
            result.append({**definition, 'override': override or None, 'resolved': resolved,
                           'config_error': error, 'config_warning': warning})
        return result

    def update_agent_definition(self, role_id, payload):
        allowed = {'name', 'responsibility', 'system_prompt', 'tools', 'enabled', 'default_binding', 'expected_version'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError('角色配置包含不支持的字段')
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', role_id):
            raise ValueError('角色 ID 使用小写字母、数字及下划线')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM agent_definitions WHERE role_id=?', (role_id,)).fetchone()
            role = _loads(row[0]) if row else {'role_id': role_id, 'version': 0, 'prompt_version': 0, 'tool_version': 0,
                'enabled': True, 'custom': True, 'tools': CHILD_TOOL_NAMES,
                'default_binding': _loads(db.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])['default_binding']}
            original = copy.deepcopy(role)
            if payload.get('expected_version') is not None and payload['expected_version'] != role['version']:
                from .store import Conflict
                raise Conflict('角色版本已更新；草稿已保留，请重新载入或确认后保存到新版本')
            for name in ('name', 'responsibility', 'system_prompt'):
                if name in payload and (not isinstance(payload[name], str) or not payload[name].strip() or len(payload[name]) > (40000 if name == 'system_prompt' else 2000)):
                    raise ValueError(f'{name} 必须是非空且长度合理的文本')
            if not row and not all(payload.get(k) for k in ('name', 'responsibility', 'system_prompt')):
                raise ValueError('新全局角色需要名称、职责及系统提示词')
            if 'enabled' in payload and not isinstance(payload['enabled'], bool):
                raise ValueError('enabled 必须是布尔值')
            if role_id == 'manager' and payload.get('enabled') is False:
                raise ValueError('主 Agent 不可归档')
            if 'tools' in payload:
                names = payload['tools']
                allowed_tools = MANAGER_TOOL_NAMES if role_id == 'manager' else CHILD_TOOL_NAMES
                if not isinstance(names, list) or any(t not in allowed_tools for t in names) or len(set(names)) != len(names):
                    raise ValueError('工具列表包含未注册工具或越权工具')
                required = 'finish_analysis' if role_id == 'manager' else 'submit_report'
                if required not in names:
                    raise ValueError('角色必须保留其报告提交工具')
            if 'default_binding' in payload:
                validated = self._validate_binding(payload['default_binding'], db, allow_effort_only=True)
                role['default_binding'] = {**role['default_binding'], **validated}
            if 'system_prompt' in payload and payload['system_prompt'] != role.get('system_prompt'):
                role['prompt_version'] += 1
            if 'tools' in payload and payload['tools'] != role.get('tools'):
                role['tool_version'] += 1
            role.update({k: v for k, v in payload.items() if k not in ('default_binding', 'expected_version')})
            if row and role == original:
                return next(r for r in self._roles(None, db, True) if r['role_id'] == role_id)
            role.update(version=role['version'] + 1, updated_at=now())
            db.execute('INSERT OR REPLACE INTO agent_definitions VALUES(?,?,?)', (role_id, role['version'], _dumps(role)))
            row = db.execute("SELECT value FROM meta WHERE key='agent_catalog_version'").fetchone()
            db.execute("INSERT OR REPLACE INTO meta VALUES('agent_catalog_version',?)", (str(int(row[0]) + 1),))
        return next(r for r in self.list_agent_roles(include_archived=True) if r['role_id'] == role_id)

    def set_account_agent_override(self, account_id, role_id, payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.account(account_id, db)
            row = db.execute('SELECT body FROM agent_definitions WHERE role_id=?', (role_id,)).fetchone()
            if not row:
                raise KeyError('统一角色不存在；账户不能新增角色')
            if not _loads(row[0])['enabled']:
                raise ValueError('角色已全局归档')
            binding = self._validate_binding(payload, db, allow_effort_only=True)
            revision = self._next_override_revision(db)
            db.execute('INSERT OR REPLACE INTO account_agent_overrides VALUES(?,?,?,?)',
                       (account_id, role_id, revision, _dumps(binding)))
        return next(r for r in self.list_agent_roles(account_id) if r['role_id'] == role_id)

    def clear_account_agent_override(self, account_id, role_id):
        with self.connect() as db:
            self.account(account_id, db)
            if not db.execute('SELECT 1 FROM agent_definitions WHERE role_id=?', (role_id,)).fetchone():
                raise KeyError('统一角色不存在')
            db.execute('DELETE FROM account_agent_overrides WHERE account_id=? AND role_id=?', (account_id, role_id))
            self._next_override_revision(db)
        return {'role_id': role_id, 'source': 'global_default', 'note': '已恢复全局默认；角色仍属于完整统一目录'}

    def _next_override_revision(self, db):
        row = db.execute("SELECT value FROM meta WHERE key='agent_override_revision'").fetchone()
        revision = int(row[0]) + 1 if row else 1
        db.execute("INSERT OR REPLACE INTO meta VALUES('agent_override_revision',?)", (str(revision),))
        return revision

    def freeze_agent_team(self, account_id=None, db=None):
        if db is None:
            with self.connect() as conn:
                return self.freeze_agent_team(account_id, conn)
        roles = self._roles(account_id, db)
        row = db.execute("SELECT value FROM meta WHERE key='agent_catalog_version'").fetchone()
        overrides = list(db.execute('SELECT role_id,revision FROM account_agent_overrides WHERE account_id=? ORDER BY role_id', (account_id or '',)))
        team = {'catalog_version': int(row[0]), 'account_id': account_id,
                'override_revision': max((r['revision'] for r in overrides), default=0),
                'roles': {r['role_id']: r for r in roles}, 'connections': {}, 'frozen_at': now()}
        for role in roles:
            pid = role['resolved']['provider_id']
            try:
                team['connections'][pid] = self._provider(pid, db)
            except KeyError:
                pass
        # A model-directory refresh, cache status, timestamps and inheritance
        # labels are observations, not changes to the effective research team.
        identity = {'roles': {}, 'connections': {}}
        for role_id, role in team['roles'].items():
            identity['roles'][role_id] = {key: role.get(key) for key in
                ('role_id', 'name', 'responsibility', 'system_prompt', 'tools', 'enabled',
                 'prompt_version', 'tool_version')}
            identity['roles'][role_id]['resolved'] = {key: role['resolved'].get(key)
                for key in ('provider_id', 'model_id', 'reasoning_effort')}
        for provider_id, provider in team['connections'].items():
            identity['connections'][provider_id] = {key: provider.get(key) for key in
                ('id', 'revision', 'credential_revision', 'base_url', 'protocol', 'archived')}
        team['team_id'] = _digest(identity)
        return team

    def runtime_role_config(self, team, role_id, base_config=None):
        role = team.get('roles', {}).get(role_id)
        if not role or not role.get('enabled'):
            raise ValueError('角色未注册或已在本轮目录中归档')
        if role.get('config_error'):
            raise ValueError(role['config_error'])
        binding = role['resolved']
        provider = team.get('connections', {}).get(binding['provider_id'])
        if not provider:
            raise ValueError('冻结供应商连接不存在')
        self.check_team_credentials(team, role_id)
        key = self._vault().get(provider.get('credential_reference'))
        config = {k: copy.deepcopy(v) for k, v in (base_config or {}).items() if k not in {'_agent_team', '_model_api_key'}}
        config.update(model_protocol=provider['protocol'], model_base_url=provider['base_url'], model_name=binding['model_id'],
                      reasoning_effort=binding['reasoning_effort'], _model_api_key=key,
                      provider_id=provider['id'], provider_revision=provider['revision'], credential_revision=provider['credential_revision'],
                      role_id=role_id, role_version=role['version'], prompt_version=role['prompt_version'], tool_version=role['tool_version'],
                      team_id=team['team_id'], account_id=team.get('account_id'),
                      _credential_fingerprint=provider.get('credential_reference', {}).get('fingerprint'))
        return config

    def check_team_credentials(self, team, role_id):
        """Request-boundary revocation check, without fetching the Key again.

        Rotation leaves previous references usable by the frozen task. Explicit
        local credential removal revokes all prior revisions; already in-flight
        HTTP requests cannot be recalled, but the next request must stop.
        """
        role = team.get('roles', {}).get(role_id)
        if not role:
            raise CredentialError('本轮角色不属于冻结目录')
        provider = team.get('connections', {}).get(role['resolved'].get('provider_id'))
        if not provider:
            raise CredentialError('冻结供应商连接不存在')
        with self.connect() as db:
            revoked = db.execute('SELECT value FROM meta WHERE key=?',
                ('revoked_provider_credentials:' + provider['id'],)).fetchone()
        if revoked and provider['credential_revision'] <= _loads(revoked[0])['through_revision']:
            raise CredentialError('本轮冻结的供应商凭据已明确撤销；后续请求已停止，请创建新任务')
        return True
