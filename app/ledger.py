"""Deterministic, append-only virtual cash-account ledger; never places orders.

All business writes run under the Store's BEGIN IMMEDIATE transaction. Decimal
strings, fee snapshots and exact remaining cost are the accounting authority;
quotes and model outputs never participate in a commit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import wraps
import hashlib
import json
import re

from .domain import TZ, now
from .calendar import trading_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_accounts(account_id TEXT PRIMARY KEY, opened_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fee_plans(account_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(account_id,version));
CREATE TABLE IF NOT EXISTS ledger_positions(account_id TEXT NOT NULL, symbol TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(account_id,symbol));
CREATE TABLE IF NOT EXISTS ledger_entries(id TEXT PRIMARY KEY, account_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL, symbol TEXT, traded_at TEXT, recorded_at TEXT NOT NULL, account_version INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(account_id,sequence));
CREATE INDEX IF NOT EXISTS ledger_entries_account ON ledger_entries(account_id,sequence);
CREATE TABLE IF NOT EXISTS cash_entries(id TEXT PRIMARY KEY, account_id TEXT NOT NULL, entry_id TEXT NOT NULL, kind TEXT NOT NULL, amount TEXT, balance TEXT, recorded_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cash_entries_account ON cash_entries(account_id,recorded_at);
CREATE TABLE IF NOT EXISTS ledger_operations(account_id TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(account_id,operation_id));
"""

SOURCES = [
    {'title': '财政部 税务总局公告2023年第39号', 'url': 'https://www.mof.gov.cn/jrttts/202308/t20230828_3904235.htm', 'effective_from': '2023-08-28', 'published_at': '2023-08-27', 'scope': '股票卖方印花税0.05%'},
    {'title': '国新证券公开收费标准（示例，非用户协议）', 'url': 'https://www.crsec.com.cn/business/standard.html', 'effective_from': None, 'published_at': None, 'scope': 'A股过户0.001%；沪深佣金包含项不同；ETF不收股票印花税和过户费'},
    {'title': '上交所收费一览表', 'url': 'https://www.sse.com.cn/services/tradingservice/charge/ssecharge/', 'effective_from': None, 'published_at': '2026-01', 'scope': '经手费由方案说明是否包含，不重复加收'},
    {'title': '深交所ETF交易说明', 'url': 'https://investor.szse.cn/warning/activities/fundIntroduction/t20210104_584079.html', 'effective_from': None, 'published_at': '2021-01-04', 'scope': 'ETF二级市场不收股票印花税'},
]


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def dec(value, *, scale=6, positive=False, signed=False):
    if isinstance(value, bool) or value is None:
        raise ValueError('金额、价格和数量须为有限十进制数')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('金额、价格和数量须为有限十进制数') from None
    if not result.is_finite() or abs(result) > Decimal('1000000000000') or (not signed and result < 0):
        raise ValueError('数值超出范围，禁止负数或非有限数')
    if positive and result <= 0:
        raise ValueError('数量或价格必须大于0')
    if scale == 0 and result != result.to_integral_value():
        raise ValueError('数量必须为整数股/份')
    if scale != 0 and result.as_tuple().exponent < -scale:
        raise ValueError(f'最多支持{scale}位小数')
    return result


def text(value):
    return format(value, 'f') if isinstance(value, Decimal) else value


def money(value):
    return format(Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), 'f')


def default_plan():
    return {'name': '常见模拟模板（待用户确认佣金协议）', 'version': 1,
        'confirmed': False, 'effective_from': '2023-08-28',
        'stock': {'commission_rate': '0.0003', 'minimum_commission': '5.00', 'transfer_included': False,
                  'transfer_rate': '0.00001', 'stamp_rate': '0.0005', 'included_fees': ['handling', 'regulatory']},
        'etf': {'commission_rate': '0.0003', 'minimum_commission': '5.00', 'transfer_included': True,
                'transfer_rate': '0', 'stamp_rate': '0', 'included_fees': ['handling']},
        'market_overrides': {}, 'sources': [{**s, 'checked_at': '2026-10-30'} for s in SOURCES],
        'rounding': 'ROUND_HALF_UP_each_component_to_cent', 'charging_unit': 'one_record_one_fee',
        'cash_scope': '模拟可用现金；未实现券商提现清算'}


def validate_plan(payload, version):
    allowed = {'name', 'confirmed', 'effective_from', 'stock', 'etf', 'market_overrides'}
    plan = default_plan()
    for k in allowed:
        if k in payload:
            plan[k] = deepcopy(payload[k])
    plan['version'] = version
    if not isinstance(plan['name'], str) or not plan['name'].strip() or len(plan['name']) > 100:
        raise ValueError('费用方案名称必填且不超过100字')
    if not isinstance(plan['confirmed'], bool):
        raise ValueError('佣金确认状态必须为布尔值')
    try:
        datetime.strptime(plan['effective_from'], '%Y-%m-%d')
    except (ValueError, TypeError):
        raise ValueError('费用方案生效日期须为YYYY-MM-DD') from None
    def check(rule, kind, partial=False):
        if not isinstance(rule, dict):
            raise ValueError('费用规则须为对象')
        fields = {'commission_rate', 'minimum_commission', 'transfer_included', 'transfer_rate', 'stamp_rate', 'included_fees', 'buy', 'sell'}
        if set(rule) - fields:
            raise ValueError('不支持的费目；管理费、托管费不按场内成交另扣')
        for key in ('commission_rate', 'transfer_rate', 'stamp_rate'):
            if key in rule:
                rate = dec(rule[key], scale=8)
                if rate > Decimal('.1'):
                    raise ValueError('费率应提交比例，如万三为0.0003')
                rule[key] = text(rate)
        if 'minimum_commission' in rule:
            rule['minimum_commission'] = money(dec(rule['minimum_commission'], scale=2))
        if 'transfer_included' in rule and not isinstance(rule['transfer_included'], bool):
            raise ValueError('过户费包含状态必须为布尔值')
        if 'included_fees' in rule and (not isinstance(rule['included_fees'], list) or set(rule['included_fees']) - {'handling', 'regulatory', 'transfer'}):
            raise ValueError('佣金包含项仅支持经手费、监管费、过户费')
        if kind == 'etf' and (Decimal(rule.get('transfer_rate', '0')) or Decimal(rule.get('stamp_rate', '0'))):
            raise ValueError('ETF二级市场不收股票印花税或股票过户费')
        for side in ('buy', 'sell'):
            if side in rule:
                if any(k in rule[side] for k in ('buy', 'sell')):
                    raise ValueError('费用方向规则不能嵌套')
                check(rule[side], kind, True)
        if not partial and not {'commission_rate', 'minimum_commission'} <= set(rule):
            raise ValueError('股票与ETF佣金率和最低费用须分别明确')
    for kind in ('stock', 'etf'):
        check(plan[kind], kind)
    if not isinstance(plan['market_overrides'], dict) or set(plan['market_overrides']) - {'SH', 'SZ'}:
        raise ValueError('本轮仅支持沪深普通现金股票/ETF账户')
    for market, kinds in plan['market_overrides'].items():
        if not isinstance(kinds, dict) or set(kinds) - {'stock', 'etf'}:
            raise ValueError('市场覆盖须按stock/etf配置')
        for kind, rule in kinds.items():
            check(rule, kind, True)
    return plan


def security_rule(security):
    sid, kind = security.get('symbol', ''), security.get('kind')
    if not re.fullmatch(r'(SH|SZ):\d{6}', sid) or kind not in ('stock', 'etf'):
        raise ValueError('本轮仅支持目录确认的沪深普通A股和场内ETF；北交所/其他品种暂不支持')
    if security.get('status') not in ('ok', 'live', 'closed', 'demo') or security.get('identity_status') in ('unverified', 'unavailable') or not security.get('name'):
        raise ValueError('证券身份尚未确认，请先通过六位代码目录核对')
    if kind == 'stock':
        if (sid.startswith('SH:') and not sid[3:].startswith(('600', '601', '603', '605'))) or (sid.startswith('SZ:') and not sid[3:].startswith(('000', '001', '002', '003', '300', '301'))):
            raise ValueError('该股票板块的交易规则尚未支持（科创板等请使用有依据的期初导入）')
        return {'kind': kind, 'settlement': 'T+1', 'buy_lot': 100, 'tick': '.01', 'unit': '股',
                'source_url': 'https://one.sse.com.cn/onething/gptz/'}
    # The exact product record is necessary for special settlement; no prefix
    # makes every ETF T+0. Ordinary domestic equity ETFs require a known subtype.
    name = security.get('canonical_name') or security['name']
    if sid == 'SH:518880':
        return {'kind': kind, 'settlement': 'T+0', 'buy_lot': 100, 'tick': '.001', 'unit': '份',
                'source_url': 'https://wap.huaan.com.cn/news/2013-07-29/175178_1.shtml'}
    domestic_equity_registry = {'SZ:159599', 'SZ:159278', 'SH:510300', 'SH:510500', 'SH:510050', 'SZ:159915', 'SZ:159919'}
    if security.get('fund_type') == 'domestic_equity' or sid in domestic_equity_registry:
        return {'kind': kind, 'settlement': 'T+1', 'buy_lot': 100, 'tick': '.001', 'unit': '份',
                'source_url': 'https://www.sse.com.cn/assortment/fund/etf/question/'}
    raise ValueError('该ETF品种的可卖规则尚未确认，请补充品种规则；不会猜测T+0或T+1')


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(TZ)
    except (ValueError, TypeError):
        raise ValueError('成交时间须包含时区，例如2026-10-30T10:00:00+08:00') from None


def available(position, on):
    return sum(int(lot['quantity']) for lot in position.get('lots', []) if lot.get('sellable_on') and lot['sellable_on'] <= on)


def accounting_precision(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 50
            return function(*args, **kwargs)
    return wrapped


class LedgerStore:
    def migrate_ledger(self, db):
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        for table in ('ledger_entries', 'cash_entries', 'fee_plans', 'ledger_operations'):
            for action in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable ledger history'); END")
        for row in db.execute('SELECT id FROM accounts').fetchall():
            self._ledger_open(db, row['id'], 'schema10_snapshot_baseline')

    def _ledger_open(self, db, account_id, source):
        from .store import uid, loads
        if db.execute('SELECT 1 FROM ledger_accounts WHERE account_id=?', (account_id,)).fetchone():
            return
        row = dict(db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone())
        holdings = loads(row['holdings'], [])
        stamp, entry_id = now(), uid()
        db.execute('INSERT INTO ledger_accounts VALUES(?,?)', (account_id, stamp))
        plan = default_plan()
        db.execute('INSERT INTO fee_plans VALUES(?,?,?,?)', (account_id, 1, dump(plan), stamp))
        self._replace_ledger_positions(db, account_id, holdings, source=source)
        body = {'id': entry_id, 'kind': 'opening', 'source': source, 'source_at': row['updated_at'],
                'recorded_at': stamp, 'account_id': account_id, 'account_name': row['name'], 'cash': row['cash'], 'holdings': holdings,
                'reason': '原快照期初基线；不推测历史交易，不追溯扣款，不推测可卖日期',
                'cash_effect': row['cash'], 'external_flow': True}
        db.execute('INSERT INTO ledger_entries VALUES(?,?,?,?,?,?,?,?,?)', (entry_id, account_id, 1, 'opening', None, None, stamp, row['version'], dump(body)))
        db.execute('INSERT INTO cash_entries VALUES(?,?,?,?,?,?,?,?)',
            (uid(), account_id, entry_id, 'opening', row['cash'], row['cash'], stamp, dump({'source': source, 'source_at': row['updated_at'], 'external_flow': True})))

    @accounting_precision
    def _replace_ledger_positions(self, db, account_id, holdings, source):
        db.execute('DELETE FROM ledger_positions WHERE account_id=?', (account_id,))
        for holding in holdings:
            qty = dec(holding['quantity'], scale=0)
            known = holding.get('cost_price') is not None
            total = text(qty * dec(holding['cost_price'])) if known else None
            sellable = holding.get('available_quantity')
            if sellable is not None:
                sellable = dec(sellable, scale=0)
                if sellable > qty:
                    raise ValueError('期初可卖数量不得超过持有数量')
            lots = []
            if sellable:
                lots.append({'quantity': text(sellable), 'sellable_on': now()[:10], 'source': source})
            if qty - (sellable or 0):
                lots.append({'quantity': text(qty - (sellable or 0)), 'sellable_on': None, 'source': source})
            body = {'symbol': holding['symbol'], 'quantity': text(qty), 'total_cost': total,
                    'cost_basis': 'known_opening_cost' if known else 'unknown_opening_cost', 'lots': lots,
                    'fees_included_in_cost': False, 'source': source}
            db.execute('INSERT INTO ledger_positions VALUES(?,?,?)', (account_id, holding['symbol'], dump(body)))

    def fee_plan(self, account_id, db=None):
        if db is None:
            with self.connect() as conn:
                return self.fee_plan(account_id, conn)
        row = db.execute('SELECT body FROM fee_plans WHERE account_id=? ORDER BY version DESC LIMIT 1', (account_id,)).fetchone()
        if not row:
            raise KeyError('账户不存在或尚未建立期初账本')
        return json.loads(row[0])

    def save_fee_plan(self, account_id, payload):
        from .store import Conflict
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.fee_plan(account_id, db)
            if payload.get('expected_fee_plan_version') != old['version']:
                raise Conflict('费用方案已变化，请重新预览并确认')
            plan = validate_plan(payload, old['version'] + 1)
            db.execute('INSERT INTO fee_plans VALUES(?,?,?,?)', (account_id, plan['version'], dump(plan), now()))
            return plan

    def _positions(self, account_id, db):
        return {r['symbol']: json.loads(r['body']) for r in db.execute('SELECT symbol,body FROM ledger_positions WHERE account_id=?', (account_id,))}

    @accounting_precision
    def ledger_account_context(self, account_id, db):
        if not db.execute('SELECT 1 FROM ledger_accounts WHERE account_id=?', (account_id,)).fetchone():
            return None
        rows = []
        for r in db.execute('SELECT body FROM ledger_entries WHERE account_id=? ORDER BY sequence DESC LIMIT 20', (account_id,)):
            entry = json.loads(r[0])
            fields = ('id', 'kind', 'symbol', 'security_name', 'side', 'quantity', 'price', 'gross', 'cash_effect',
                      'cash_before', 'cash_after', 'realized_pnl', 'recorded_at', 'traded_at', 'reason', 'mode',
                      'reference_entry_id', 'account_version', 'external_flow')
            rows.append({**{k: entry[k] for k in fields if k in entry},
                         'fee_total': (entry.get('fees') or {}).get('total'),
                         'fee_estimated': (entry.get('fees') or {}).get('estimated_only')})
        total_fees, realized, unknown = Decimal(0), Decimal(0), 0
        for row in db.execute('SELECT body FROM ledger_entries WHERE account_id=?', (account_id,)):
            e = json.loads(row[0])
            total_fees += Decimal(e.get('fee_effect', '0'))
            if e.get('realized_pnl_effect') is not None:
                realized += Decimal(e['realized_pnl_effect'])
            elif e.get('kind') == 'trade' and e.get('side') == 'sell':
                unknown += 1
            unknown += int(e.get('unknown_realized_effect', 0))
        return {'accounting_method': 'moving_weighted_average', 'fees_included_in_cost': True,
                'cash_scope': '模拟可用现金；卖出可用不代表可取', 'total_fees': money(total_fees),
                'known_realized_pnl': money(realized), 'realized_pnl': money(realized) if not unknown else None,
                'unknown_cost_sales': unknown, 'recent_entries': rows,
                'positions': self._positions(account_id, db)}

    def ledger(self, account_id):
        with self.connect() as db:
            account = self.account(account_id, db)
            positions = self._positions(account_id, db)
            for pos in positions.values():
                pos['available_quantity'] = str(available(pos, now()[:10]))
                pos['availability_unknown_quantity'] = str(sum(int(l['quantity']) for l in pos['lots'] if l.get('sellable_on') is None))
            entries = [json.loads(r[0]) for r in db.execute('SELECT body FROM ledger_entries WHERE account_id=? ORDER BY sequence DESC LIMIT 200', (account_id,))]
            cash = [{**dict(r), 'body': json.loads(r['body'])} for r in db.execute('SELECT * FROM cash_entries WHERE account_id=? ORDER BY recorded_at DESC LIMIT 200', (account_id,))]
            return {'account_id': account_id, 'input_version': account['version'], 'cash': account['cash'],
                    'positions': list(positions.values()), 'fee_plan': self.fee_plan(account_id, db),
                    'entries': entries, 'cash_entries': cash, 'summary': self.ledger_account_context(account_id, db)}

    def _operation(self, db, account_id, payload, action):
        from .store import Conflict
        operation = payload.get('operation_id')
        if not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', operation):
            raise ValueError('记账必须提供8至128位稳定operation_id；响应未知时先查询回执')
        fingerprint = hashlib.sha256(dump({'action': action, 'payload': payload}).encode()).hexdigest()
        old = db.execute('SELECT * FROM ledger_operations WHERE account_id=? AND operation_id=?', (account_id, operation)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise Conflict('此操作ID已用于不同记账输入，请核对原回执')
            return fingerprint, {**json.loads(old['receipt']), 'replayed': True}
        return fingerprint, None

    def ledger_operation(self, account_id, operation_id):
        with self.connect() as db:
            row = db.execute('SELECT receipt FROM ledger_operations WHERE account_id=? AND operation_id=?', (account_id, operation_id)).fetchone()
            if not row:
                raise KeyError('未找到记账回执；请继续只读核对，勿盲目重复提交')
            return {**json.loads(row[0]), 'replayed': True}

    def _version(self, account, payload):
        from .store import Conflict
        if isinstance(payload.get('expected_version'), bool) or payload.get('expected_version') != account['version']:
            raise Conflict('账户版本已变化，请重新预览并确认现金与持仓')

    @accounting_precision
    def _trade(self, db, account_id, payload):
        from .store import Conflict
        account = self.account(account_id, db)
        self._version(account, payload)
        plan = self.fee_plan(account_id, db)
        if payload.get('fee_plan_version') != plan['version']:
            raise Conflict('费用方案版本已变化，请重新预览并确认')
        sid = payload.get('symbol')
        row = db.execute('SELECT body FROM securities WHERE symbol=?', (sid,)).fetchone()
        if not row:
            raise ValueError('证券身份尚未确认，请先通过六位代码目录核对')
        security = json.loads(row[0])
        rule = security_rule(security)
        qty = dec(payload.get('quantity'), scale=0, positive=True)
        price = dec(payload.get('price'), scale=3 if rule['kind'] == 'etf' else 2, positive=True)
        if price % Decimal(rule['tick']):
            raise ValueError('成交价不符合该品种最小价格变动单位')
        side = payload.get('side')
        if side not in ('buy', 'sell'):
            raise ValueError('买卖方向须为buy或sell')
        mode = payload.get('mode')
        if mode not in ('simulated', 'historical'):
            raise ValueError('请选择模拟成交或历史补录；本应用不会向交易所下单')
        at = timestamp(payload.get('traded_at'))
        if at > datetime.now(TZ) + timedelta(minutes=1):
            raise ValueError('不能记录未来成交')
        day = at.date().isoformat()
        if day < plan['effective_from']:
            raise ValueError('成交日期早于所选费用方案生效日，请选择适用方案或期初校正')
        if mode == 'historical' and trading_date(day).get('trading_day') is not True:
            raise ValueError('历史成交日期须有已核实的交易日；休市验证请选择模拟记账')
        # Arbitrary insertion would require replaying cost/availability and every
        # later version. This release only appends chronologically per account.
        previous = db.execute("""SELECT e.traded_at FROM ledger_entries e WHERE e.account_id=? AND e.kind='trade'
            AND NOT EXISTS(SELECT 1 FROM ledger_entries r WHERE r.account_id=e.account_id AND r.kind='reversal' AND json_extract(r.body,'$.reference_entry_id')=e.id)
            ORDER BY e.sequence DESC LIMIT 1""", (account_id,)).fetchone()
        if previous and at < timestamp(previous[0]):
            raise ValueError('不支持在已记账成交之前插单；请使用有依据的期初校正或先撤销末笔')
        if account['cash'] is None:
            raise ValueError('现金未知，请先录入有来源的期初现金；不会自动当作0')
        before = self._positions(account_id, db)
        position = deepcopy(before.get(sid, {'symbol': sid, 'quantity': '0', 'total_cost': '0', 'lots': [], 'cost_basis': 'trade_cost', 'fees_included_in_cost': True}))
        old_qty = Decimal(position['quantity'])
        if side == 'buy' and int(qty) % rule['buy_lot']:
            raise ValueError(f'该品种买入须为{rule["buy_lot"]}股/份的整数倍；数量单位不是手')
        sellable = available(position, day)
        if side == 'sell':
            if qty > old_qty:
                raise ValueError('卖出数量超过持有量，禁止做空')
            if qty > sellable:
                raise ValueError(f'可卖数量不足：当前可卖{sellable}，含T+1未解锁或期初可卖依据未知；请勿推测历史买入日期')
            # One odd-lot remainder must be sold in one go, not split repeatedly.
            if int(qty) % 100 and qty != sellable:
                raise ValueError('零股/零份须一次卖出全部当前可卖余量')
        settings = deepcopy(plan[rule['kind']])
        override = plan.get('market_overrides', {}).get(sid[:2], {}).get(rule['kind'], {})
        settings.update({k: v for k, v in override.items() if k not in ('buy', 'sell')})
        settings.update(plan[rule['kind']].get(side, {}))
        settings.update(override.get(side, {}))
        gross_raw = qty * price
        gross = Decimal(money(gross_raw))
        raw = {'commission': max(gross * Decimal(settings['commission_rate']), Decimal(settings['minimum_commission'])),
               'transfer': gross * Decimal(settings.get('transfer_rate', '0.00001')) if rule['kind'] == 'stock' and not settings.get('transfer_included') and 'transfer' not in settings.get('included_fees', []) else Decimal(0),
               'stamp': gross * Decimal(settings.get('stamp_rate', '0.0005')) if rule['kind'] == 'stock' and side == 'sell' else Decimal(0)}
        estimated = {k: money(v) for k, v in raw.items()}
        actual = dict(estimated)
        fee_override = payload.get('fees_override')
        if fee_override is not None:
            if not isinstance(fee_override, dict) or not isinstance(fee_override.get('reason'), str) or not fee_override['reason'].strip():
                raise ValueError('单笔实际费用覆盖必须填写交割单等依据')
            if set(fee_override) - {'commission', 'transfer', 'stamp', 'reason'}:
                raise ValueError('单笔费用覆盖包含未知费目')
            for key in ('commission', 'transfer', 'stamp'):
                if key in fee_override:
                    actual[key] = money(dec(fee_override[key], scale=2))
            if Decimal(actual['transfer']) and (settings.get('transfer_included') or 'transfer' in settings.get('included_fees', [])):
                raise ValueError('过户费已包含佣金，不可再次另扣；请先明确实际费用方案')
            if (rule['kind'] == 'etf' and (Decimal(actual['stamp']) or Decimal(actual['transfer']))) or (side == 'buy' and Decimal(actual['stamp'])):
                raise ValueError('该品种/方向不适用股票印花税或过户费')
        fee_total = sum(Decimal(v) for v in actual.values())
        delta = -gross - fee_total if side == 'buy' else gross - fee_total
        cash = Decimal(account['cash']) + delta
        if cash < 0:
            raise ValueError('可用现金不足，禁止融资或负现金')
        new_qty = old_qty + qty if side == 'buy' else old_qty - qty
        cost = Decimal(position['total_cost']) if position.get('total_cost') is not None else None
        realized, relieved = None, None
        if side == 'buy':
            if cost is not None:
                cost += gross + fee_total
            sellable_on = day
            if rule['settlement'] == 'T+1':
                next_day = at.date()
                for _ in range(30):
                    next_day += timedelta(days=1)
                    check = trading_date(next_day)
                    if check.get('trading_day') is True:
                        sellable_on = next_day.isoformat()
                        break
                    if check.get('trading_day') is None:
                        raise ValueError('缺少下一交易日历，不能确认T+1可卖日')
                else:
                    raise ValueError('无法确认下一可卖交易日')
            position['lots'].append({'quantity': text(qty), 'sellable_on': sellable_on, 'source': 'trade', 'traded_at': at.isoformat()})
        else:
            with localcontext() as context:
                context.prec = 50
                if cost is not None:
                    relieved = cost if qty == old_qty else cost * qty / old_qty
                    realized = gross - fee_total - relieved
                    cost -= relieved
            remaining = int(qty)
            for lot in position['lots']:
                if remaining and lot.get('sellable_on') and lot['sellable_on'] <= day:
                    used = min(remaining, int(lot['quantity']))
                    lot['quantity'] = str(int(lot['quantity']) - used)
                    remaining -= used
            position['lots'] = [lot for lot in position['lots'] if int(lot['quantity'])]
        if not new_qty:
            cost, position['lots'] = Decimal(0), []
        position.update(quantity=text(new_qty), total_cost=text(cost), fees_included_in_cost=True)
        positions = {**before, sid: position}
        holdings = []
        for symbol, pos in positions.items():
            if Decimal(pos['quantity']):
                unit = Decimal(pos['total_cost']) / Decimal(pos['quantity']) if pos.get('total_cost') is not None else None
                holdings.append({'symbol': symbol, 'quantity': pos['quantity'], 'cost_price': text(unit)})
        return {'kind': 'trade', 'side': side, 'symbol': sid, 'security_name': security.get('canonical_name') or security['name'],
            'quantity': text(qty), 'price': text(price), 'traded_at': at.isoformat(), 'mode': mode,
            'gross': money(gross), 'gross_raw': text(gross_raw), 'gross_rounding': 'ROUND_HALF_UP_to_cent', 'fees': {'components': actual, 'estimated': estimated, 'raw': {k: text(v) for k, v in raw.items()}, 'total': money(fee_total),
                'plan_version': plan['version'], 'plan_snapshot': plan, 'rule': settings, 'override': fee_override,
                'estimated_only': not plan['confirmed'] and fee_override is None, 'included_in_cost': side == 'buy'},
            'fee_effect': money(fee_total), 'cash_effect': money(delta), 'cash_before': account['cash'], 'cash_after': money(cash),
            'quantity_before': text(old_qty), 'quantity_after': text(new_qty), 'available_before': str(sellable),
            'total_cost_after': text(cost), 'cost_relieved': text(relieved), 'realized_pnl': money(realized) if realized is not None else None,
            'realized_pnl_effect': text(realized), 'cost_basis': 'known' if realized is not None or side == 'buy' and cost is not None else '成本依据不足',
            'rule': rule, 'note': str(payload.get('note', ''))[:1000], 'before_positions': before,
            'after_positions': positions, 'before_holdings': account['holdings'], 'after_holdings': holdings,
            'input_version': account['version'], 'operation_id': payload.get('operation_id')}

    def preview_trade(self, account_id, payload):
        with self.connect() as db:
            db.execute('BEGIN')
            result = self._trade(db, account_id, payload)
            return {k: v for k, v in result.items() if k not in ('before_positions', 'after_positions', 'before_holdings', 'after_holdings')}

    def _commit_entry(self, db, account_id, payload, entry, fingerprint):
        from .store import uid
        account = self.account(account_id, db)
        stamp, entry_id = now(), uid()
        entry.update(id=entry_id, recorded_at=stamp, operation_id=payload['operation_id'], reason=entry.get('reason') or str(payload.get('reason', ''))[:1000])
        sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM ledger_entries WHERE account_id=?', (account_id,)).fetchone()[0]
        entry['sequence'] = sequence
        data = {**account, 'cash': entry['cash_after'], 'holdings': entry['after_holdings'], 'expected_version': account['version'],
                'name': entry.get('after_name', account['name']), 'preferences': entry.get('after_preferences', account['preferences'])}
        # Position state is in the same transaction as account, snapshot, demand,
        # queue and receipt. A crash rolls all of them back together.
        db.execute('DELETE FROM ledger_positions WHERE account_id=?', (account_id,))
        for sid, pos in entry['after_positions'].items():
            db.execute('INSERT INTO ledger_positions VALUES(?,?,?)', (account_id, sid, dump(pos)))
        entry['account_version'] = account['version'] + 1
        db.execute('INSERT INTO ledger_entries VALUES(?,?,?,?,?,?,?,?,?)', (entry_id, account_id, sequence, entry['kind'], entry.get('symbol'), entry.get('traded_at'), stamp, entry['account_version'], dump(entry)))
        db.execute('INSERT INTO cash_entries VALUES(?,?,?,?,?,?,?,?)', (uid(), account_id, entry_id, entry['kind'], entry.get('cash_effect'), entry['cash_after'], stamp,
            dump({'external_flow': entry.get('external_flow', False), 'reason': entry.get('reason'), 'reference_entry_id': entry.get('reference_entry_id')})))
        db.execute("INSERT INTO research_demands(event_key,account_id,input_version,kind,reason,state,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                   ('ledger:' + entry_id, account_id, entry['account_version'], 'ledger_update', 'ledger:' + entry['kind'] + ':' + entry_id, stamp, stamp))
        saved = self.save_account(data, account_id, reason='ledger_' + entry['kind'], db=db)
        job_id = saved['latest_job_id']
        receipt = {'entry_id': entry_id, 'trade_id': entry_id if entry['kind'] == 'trade' else None, 'account_id': account_id,
                   'input_version': saved['version'], 'operation_id': payload['operation_id'], 'entry': entry,
                   'cash_before': entry['cash_before'], 'cash_after': entry['cash_after'], 'fees': entry.get('fees'),
                   'quantity_before': entry.get('quantity_before'), 'quantity_after': entry.get('quantity_after'),
                   'research': {'job_id': job_id, 'state': 'queued', 'message': '记账成功；分析待处理，分析失败不会回滚交易'},
                   'replayed': False}
        db.execute('INSERT INTO ledger_operations VALUES(?,?,?,?,?)', (account_id, payload['operation_id'], fingerprint, dump(receipt), stamp))
        return receipt

    def record_trade(self, account_id, payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint, old = self._operation(db, account_id, payload, 'trade')
            if old:
                return old
            return self._commit_entry(db, account_id, payload, self._trade(db, account_id, payload), fingerprint)

    def adjust_funds(self, account_id, payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint, old = self._operation(db, account_id, payload, 'funds')
            if old:
                return old
            account = self.account(account_id, db)
            self._version(account, payload)
            if not str(payload.get('reason', '')).strip():
                raise ValueError('资金调整必须填写来源或原因')
            amount = dec(payload.get('amount'), scale=2, positive=True)
            kind = payload.get('kind')
            if kind not in ('deposit', 'withdraw', 'opening'):
                raise ValueError('资金类型须为deposit、withdraw或opening')
            if kind == 'opening' and account['cash'] is not None:
                raise ValueError('期初现金已存在，请使用转入/转出或明确校正')
            if kind != 'opening' and account['cash'] is None:
                raise ValueError('现金未知，请先录入有来源的期初现金')
            cash = Decimal(account['cash'] or '0')
            delta = -amount if kind == 'withdraw' else amount
            if cash + delta < 0:
                raise ValueError('资金转出超过模拟可用现金；本系统不处理券商提现清算')
            positions = self._positions(account_id, db)
            entry = {'kind': kind, 'cash_before': account['cash'], 'cash_after': money(cash + delta), 'cash_effect': money(delta),
                     'external_flow': True, 'before_positions': positions, 'after_positions': positions,
                     'before_holdings': account['holdings'], 'after_holdings': account['holdings']}
            return self._commit_entry(db, account_id, payload, entry, fingerprint)

    def correct_snapshot(self, account_id, payload, db=None):
        if db is None:
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return self.correct_snapshot(account_id, payload, conn)
        fingerprint, old = self._operation(db, account_id, payload, 'correction')
        if old:
            return old
        account = self.account(account_id, db)
        self._version(account, payload)
        if not str(payload.get('reason', '')).strip():
            raise ValueError('期初/校正必须填写依据；这不是交易，不按现价扣款')
        before = self._positions(account_id, db)
        holdings = payload.get('holdings', account['holdings'])
        if not isinstance(holdings, list) or len(holdings) > 300:
            raise ValueError('持仓校正格式错误')
        seen = set()
        for h in holdings:
            if not isinstance(h, dict) or not re.fullmatch(r'(SH|SZ|BJ):\d{6}', h.get('symbol', '')) or h['symbol'] in seen:
                raise ValueError('持仓校正需要唯一规范证券代码')
            seen.add(h['symbol'])
            dec(h.get('quantity'), scale=0)
            if h.get('cost_price') is not None:
                dec(h['cost_price'])
        cash_value = payload.get('cash', account['cash'])
        cash = money(dec(cash_value, scale=2)) if cash_value is not None else None
        self._replace_ledger_positions(db, account_id, holdings, source='explicit_correction')
        after = self._positions(account_id, db)
        cash_effect = money(Decimal(cash) - Decimal(account['cash'])) if cash is not None and account['cash'] is not None else None
        entry = {'kind': 'correction', 'cash_before': account['cash'], 'cash_after': cash, 'cash_effect': cash_effect,
                 'external_flow': True, 'before_positions': before, 'after_positions': after,
                 'before_holdings': account['holdings'], 'after_holdings': holdings,
                 'reason': payload['reason'], 'actor': 'local_user', 'historical_pnl_rewritten': False,
                 'before_name': account['name'], 'after_name': payload.get('name', account['name']),
                 'before_preferences': account['preferences'], 'after_preferences': payload.get('preferences', account['preferences'])}
        return self._commit_entry(db, account_id, payload, entry, fingerprint)

    def reverse_last_entry(self, account_id, entry_id, payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint, old = self._operation(db, account_id, payload, 'reverse:' + entry_id)
            if old:
                return old
            account = self.account(account_id, db)
            self._version(account, payload)
            if not str(payload.get('reason', '')).strip():
                raise ValueError('撤销必须填写原因')
            latest = db.execute('SELECT id,kind,body FROM ledger_entries WHERE account_id=? ORDER BY sequence DESC LIMIT 1', (account_id,)).fetchone()
            if not latest or latest['id'] != entry_id or latest['kind'] in ('opening', 'reversal'):
                raise ValueError('仅支持撤销账户末笔非期初记录；更早记录请使用有依据的校正，不进行任意历史重算')
            original = json.loads(latest['body'])
            entry = {'kind': 'reversal', 'reference_entry_id': entry_id,
                     'cash_before': account['cash'], 'cash_after': original['cash_before'],
                     'cash_effect': money(-Decimal(original['cash_effect'])) if original.get('cash_effect') is not None else None,
                     'external_flow': original.get('external_flow', False), 'fee_effect': money(-Decimal(original.get('fee_effect', '0'))),
                     'realized_pnl_effect': text(-Decimal(original['realized_pnl_effect'])) if original.get('realized_pnl_effect') is not None else None,
                     'unknown_realized_effect': -1 if original.get('kind') == 'trade' and original.get('side') == 'sell' and original.get('realized_pnl_effect') is None else 0,
                     'before_positions': self._positions(account_id, db), 'after_positions': original['before_positions'],
                     'before_holdings': account['holdings'], 'after_holdings': original['before_holdings'],
                     'no_new_trading_fees': True,
                     'before_name': account['name'], 'after_name': original.get('before_name', account['name']),
                     'before_preferences': account['preferences'], 'after_preferences': original.get('before_preferences', account['preferences'])}
            return self._commit_entry(db, account_id, payload, entry, fingerprint)

    def adjust_trade_fees(self, account_id, entry_id, payload):
        """Append a supported last-fill fee correction; never replay old costs."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fingerprint, old = self._operation(db, account_id, payload, 'fees:' + entry_id)
            if old:
                return old
            account = self.account(account_id, db)
            self._version(account, payload)
            if not str(payload.get('reason', '')).strip():
                raise ValueError('实际费用更正必须填写交割单等依据')
            latest = db.execute('SELECT id,kind,body FROM ledger_entries WHERE account_id=? ORDER BY sequence DESC LIMIT 1', (account_id,)).fetchone()
            if not latest or latest['id'] != entry_id or latest['kind'] != 'trade':
                raise ValueError('仅支持末笔交易的费用更正；更早交易影响后续成本，暂不支持任意历史重算')
            original = json.loads(latest['body'])
            supplied = payload.get('fees')
            if not isinstance(supplied, dict) or not supplied or set(supplied) - {'commission', 'transfer', 'stamp'}:
                raise ValueError('实际费用须明确commission、transfer或stamp分项')
            actual = {**original['fees']['components'], **{key: money(dec(value, scale=2)) for key, value in supplied.items()}}
            rule, kind, side = original['fees']['rule'], original['rule']['kind'], original['side']
            if Decimal(actual['transfer']) and (rule.get('transfer_included') or 'transfer' in rule.get('included_fees', [])):
                raise ValueError('过户费已包含佣金，不可再次另扣')
            if kind == 'etf' and (Decimal(actual['transfer']) or Decimal(actual['stamp'])) or side == 'buy' and Decimal(actual['stamp']):
                raise ValueError('该品种/方向不适用股票印花税或过户费')
            actual_total = sum(Decimal(value) for value in actual.values())
            previous_total = Decimal(original['fees']['total'])
            delta = actual_total - previous_total
            if not delta and actual == original['fees']['components']:
                raise ValueError('费用未变化，无需重复更正')
            cash = Decimal(account['cash']) - delta
            if cash < 0:
                raise ValueError('补扣费用后现金不足，禁止负现金')
            before = self._positions(account_id, db)
            after = deepcopy(before)
            position = after[original['symbol']]
            realized_effect = None
            if side == 'buy' and position.get('total_cost') is not None:
                with localcontext() as context:
                    context.prec = 50
                    position['total_cost'] = text(Decimal(position['total_cost']) + delta)
            elif side == 'sell' and original.get('realized_pnl_effect') is not None:
                realized_effect = text(-delta)
            holdings = []
            for sid, pos in after.items():
                if Decimal(pos['quantity']):
                    with localcontext() as context:
                        context.prec = 50
                        unit = Decimal(pos['total_cost']) / Decimal(pos['quantity']) if pos.get('total_cost') is not None else None
                    holdings.append({'symbol': sid, 'quantity': pos['quantity'], 'cost_price': text(unit)})
            entry = {'kind': 'fee_adjustment', 'symbol': original['symbol'], 'side': side,
                'reference_entry_id': entry_id, 'reason': payload['reason'], 'actor': 'local_user',
                'fees': {'components': actual, 'previous': original['fees']['components'],
                         'original_estimate': original['fees']['estimated'], 'total': money(actual_total),
                         'previous_total': money(previous_total), 'delta': money(delta),
                         'plan_version': original['fees']['plan_version'], 'estimated_only': False},
                'fee_effect': money(delta), 'cash_before': account['cash'], 'cash_after': money(cash),
                'cash_effect': money(-delta), 'realized_pnl_effect': realized_effect,
                'total_cost_after': position.get('total_cost'), 'no_new_trading_fees': True,
                'before_positions': before, 'after_positions': after,
                'before_holdings': account['holdings'], 'after_holdings': holdings}
            return self._commit_entry(db, account_id, payload, entry, fingerprint)
