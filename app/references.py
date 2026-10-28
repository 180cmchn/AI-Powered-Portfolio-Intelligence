"""Mechanical reference location only; no credibility or investment admission rules."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from .materials import index_of, parent_material_ids


def source_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


class ReferenceStore:
    def resolve_result_references(self, result, account_id=None, db=None):
        """Resolve stored IDs without fetching, granting access, or claiming a read.

        A source URL alone is a link, not evidence that its destination was read.
        Unknown and inaccessible IDs deliberately have the same public response.
        Server-owned references replace any model-supplied resolution metadata.
        """
        from .store import loads
        if db is None:
            with self.connect() as conn:
                return self.resolve_result_references(result, account_id, conn)

        native_sources = None
        def url_key(url):
            return urlsplit(url)._replace(fragment='').geturl()

        def saved_native_source(url):
            nonlocal native_sources
            if native_sources is None:
                native_sources = {}
                rows = db.execute("""SELECT m.id,m.body FROM materials m
                    JOIN material_catalog c ON c.material_id=m.id
                    JOIN material_representations r ON r.material_id=m.id
                    JOIN material_document_versions d ON d.id=r.document_version_id
                    WHERE d.representation_kind='native_search_source' AND
                    (c.scope='public' OR (?<>'' AND c.account_id=?))
                    ORDER BY m.created_at DESC,m.rowid DESC""", (account_id or '', account_id or '')).fetchall()
                for row in rows:
                    body = loads(row['body'])
                    stored_url = source_url(body.get('url'))
                    if stored_url:
                        native_sources.setdefault(url_key(stored_url), row['id'])
            return native_sources.get(url_key(url))

        def resolve(material_id, ancestry=()):
            if material_id in ancestry or len(ancestry) >= 5:
                return {'material_id': material_id, 'status': 'unresolved', 'reason': 'reference_chain_not_resolved'}
            row = db.execute('SELECT body FROM materials WHERE id=?', (material_id,)).fetchone() if self.material_accessible(material_id, account_id, db) else None
            if row is None:
                return {'material_id': material_id, 'status': 'unresolved', 'reason': 'unknown_or_inaccessible'}
            body = loads(row[0])
            meta = index_of(body)
            url = source_url(body.get('url'))
            if body.get('access') == 'failed':
                return {'material_id': material_id, 'url': url,
                        'status': 'source_link' if url else 'unresolved', 'reason': 'saved_retrieval_failed',
                        'access': 'failed'}
            if body.get('representation') == 'native_search_source' and body.get('access') == 'source_link':
                return {'material_id': material_id, 'url': url, 'title': meta.get('source_title') or meta.get('title'),
                        'status': 'source_link' if url else 'unresolved', 'reason': 'native_search_link_only',
                        'access': 'source_link', 'representation_kind': 'native_search_source',
                        'scope': meta.get('scope'),
                        'document_identity_kind': meta.get('document_identity_kind'),
                        'range': {'read_completeness': 'source_link_only'}}
            ref = {'material_id': material_id, 'status': 'resolved',
                    'title': meta.get('source_title') or meta.get('title'), 'url': url,
                    'scope': meta.get('scope'), 'access': meta.get('access', 'unknown'),
                    'classification': meta.get('classification'),
                    'document_version': meta.get('document_version'),
                    'document_identity_kind': meta.get('document_identity_kind', 'legacy_unknown'),
                    'representation_kind': meta.get('representation_kind', 'legacy_unknown'),
                    'range': {k: meta[k] for k in ('returned_range', 'requested_range', 'pages', 'char_count', 'truncated', 'page_range',
                              'offset', 'returned_chars', 'requested_chars', 'read_completeness') if k in meta},
                    'locator': '/api/materials/' + quote(material_id, safe='') + '/range' +
                               ('?account_id=' + quote(account_id, safe='') if account_id else '')}
            if meta.get('representation_kind') in ('agent_extraction', 'generated_analysis'):
                parent_ids = parent_material_ids(body)
                ref['origin_references'] = [resolve(mid, (*ancestry, material_id)) for mid in dict.fromkeys(parent_ids)]
                ref['origin_status'] = 'linked_materials' if parent_ids else 'unlinked'
                # Claimed source pages are model metadata, not provider-confirmed
                # access ranges. Keep the stored extraction's own length separate.
                ref['claimed_source_range'] = ref['range']
                ref['range'] = {'char_count': meta['char_count'], 'read_completeness': 'agent_authored'}
            return ref

        def walk(value):
            if isinstance(value, list):
                return [walk(item) for item in value]
            if not isinstance(value, dict):
                return value
            item = {key: walk(val) for key, val in value.items() if key not in ('references', 'reference_summary')}
            ids = parent_material_ids(item)
            if isinstance(item.get('material_id'), str):
                ids = [item['material_id'], *ids]
            refs = [resolve(mid) for mid in dict.fromkeys(ids)]
            urls = []
            for key in ('url', 'source_url'):
                if source_url(item.get(key)):
                    urls.append(item[key])
            for key in ('source_urls', 'urls'):
                if isinstance(item.get(key), list):
                    urls.extend(v for v in item[key] if source_url(v))
            for key in ('report', 'summary', 'basis'):
                if isinstance(item.get(key), str):
                    urls.extend(v.rstrip('.,;，。；') for v in re.findall(r'https?://[^\s<>\]\)"\u3000]+', item[key]))
            known = {url_key(ref['url']) for ref in refs if source_url(ref.get('url'))}
            for url in dict.fromkeys(urls):
                if not source_url(url) or url_key(url) in known:
                    continue
                mid = saved_native_source(url)
                ref = resolve(mid) if mid else {'url': url, 'status': 'source_link', 'reason': 'not_located_in_saved_material'}
                if mid and ref.get('url') != url:
                    ref['citation_url'] = url
                refs.append(ref)
                known.add(url_key(url))
            if refs or any(k in item for k in ('report', 'material_ids', 'source_material_ids', 'material_id')):
                item['references'] = refs
            return item

        resolved = walk(result)
        counts = {'resolved': 0, 'source_link': 0, 'unresolved': 0}
        def count_ref(ref):
            counts[ref['status']] += 1
            for parent in ref.get('origin_references', []):
                count_ref(parent)
        def count(value):
            if isinstance(value, dict):
                for ref in value.get('references', []):
                    count_ref(ref)
                for key, val in value.items():
                    if key != 'references':
                        count(val)
            elif isinstance(value, list):
                for val in value:
                    count(val)
        count(resolved)
        resolved['reference_summary'] = counts
        return resolved
