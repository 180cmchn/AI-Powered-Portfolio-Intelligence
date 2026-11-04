"""Read-only server labels and exports; never rewrite model-authored report bytes."""
from __future__ import annotations

import hashlib
import json


KIND_LABELS = {'formal': '正式固定批次', 'exploratory': '探索记录',
               'test': '工程测试夹具', 'demo': '演示', 'none': '无预测批次'}
KIND_NOTES = {
    'formal': '正式固定批次；只有符合规则的已到期方向预测进入该正式规则的准确率分母，弃权单列。',
    'exploratory': '探索记录；不进入正式固定批次准确率，正文中的“正式”措辞不改变记录类型。',
    'test': '工程测试夹具；不代表真实前瞻表现，不进入正式准确率。',
    'demo': '演示结果；不代表真实模型研究或真实前瞻表现，不进入正式准确率。',
    'none': '此报告没有可核对的预测批次；不根据正文推断或补写预测类型。',
}


def prediction_context(context):
    """Constant-size identity; targets already belong to the retained seed/archive.

    Repeating a whole target page here would bypass ModelClient.session's seed
    bounds and consume the reserved value-first calculation budget.
    """
    batch = context.get('forecast_batch')
    batch = batch if isinstance(batch, dict) else {}
    kind = batch.get('kind', 'none')
    return {'kind': kind,
        'batch_id': batch.get('id'), 'base_date': batch.get('base_date'),
        'deadline': batch.get('deadline'),
        'publication_time': None, 'information_cutoff': None,
        'target_page_count': len(batch.get('items', [])),
        'targets_has_more': bool(batch.get('has_more')),
        'targets_location': 'forecast_batch.items（种子截断或批次截断见 record_id）；未读目标由主 Agent 分页 list_forecast_targets。',
        'instruction': 'kind由服务端锁定，正文不能升级；仅formal属正式。发布前时点为null；期限是交易日收益间隔。'}


def attach_report_metadata(store, report):
    """Historical publication and binding rows are authoritative, not prose/config."""
    with store.connect() as db:
        batches = [dict(row) for row in db.execute('''SELECT DISTINCT b.id,b.kind,b.base_date,b.deadline,b.rule_version
            FROM forecast_batches b WHERE b.account_id=? AND
            (b.job_id=? OR b.id IN (SELECT batch_id FROM forecast_job_bindings WHERE job_id=?)
             OR b.id IN (SELECT batch_id FROM forecasts WHERE report_id=?)) ORDER BY b.base_date,b.id''',
            (report['account_id'], report['job_id'], report['job_id'], report['id']))]
        for batch in batches:
            batch['label'] = KIND_LABELS.get(batch['kind'], '未知类型')
            batch['note'] = KIND_NOTES.get(batch['kind'], '类型未核实，不推断为正式。')
            batch['targets'] = [dict(row) for row in db.execute('''SELECT s.symbol,s.horizon,s.target_date,
                f.id AS forecast_id,f.kind,f.published_at,f.information_cutoff,f.direction
                FROM forecast_samples s LEFT JOIN forecasts f ON f.sample_id=s.id AND f.report_id=?
                WHERE s.batch_id=? ORDER BY s.symbol,s.horizon''', (report['id'], batch['id']))]
    kinds = ['demo'] if report.get('mode') == 'demo' else sorted({b['kind'] for b in batches}) or ['none']
    metadata = {'source': 'server_persisted_forecast_identity', 'kinds': kinds,
        'labels': [KIND_LABELS.get(kind, '未知类型') for kind in kinds],
        'notes': [KIND_NOTES.get(kind, '类型未核实，不推断为正式。') for kind in kinds],
        'report_id': report['id'], 'job_id': report['job_id'], 'input_version': report['input_version'],
        'report_published_at': report['created_at'], 'data_as_of': report.get('data_as_of'),
        'batches': batches,
        'time_note': '基准日是收益起点；发布时间是实际保存时点；information_cutoff 为预测发布时记录的信息截止，不保证所有引用材料均采集于该时点。data_as_of 是报告资料/估值时点。'}
    return {**report, 'system_metadata': metadata}


def export_report(report, format='reading'):
    original = report['report'].encode('utf-8')
    original_sha256 = hashlib.sha256(original).hexdigest()
    if format == 'original':
        content = original
    elif format == 'reading':
        metadata = report['system_metadata']
        labels = '、'.join(metadata['labels'])
        prefix = (f'# 研究报告 · {labels}\n\n系统标识（不修改下方模型原文）：\n\n'
                  + '\n'.join('- ' + note for note in metadata['notes'])
                  + f'\n\n原文 UTF-8 SHA-256：`{original_sha256}`\n\n'
                  + '```json\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n```\n\n---\n\n')
        content = prefix.encode('utf-8') + original
    else:
        raise ValueError('导出类型须为 original 或 reading')
    return {'content': content, 'sha256': hashlib.sha256(content).hexdigest(),
            'original_sha256': original_sha256, 'filename': f"report-{report['id']}-{format}.md"}
