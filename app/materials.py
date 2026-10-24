"""Immutable source versions and bounded material indexes, without source admission rules."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from .domain import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS material_catalog(
 material_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, version INTEGER NOT NULL,
 previous_id TEXT, content_hash TEXT NOT NULL, account_id TEXT NOT NULL,
 scope TEXT NOT NULL, summary TEXT NOT NULL, metadata TEXT NOT NULL, char_count INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS material_sources ON material_catalog(source_key,version);
CREATE INDEX IF NOT EXISTS material_hashes ON material_catalog(content_hash);
CREATE INDEX IF NOT EXISTS uses_account ON material_uses(account_id,material_id);
CREATE TABLE IF NOT EXISTS material_notifications(
 material_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT);
CREATE TABLE IF NOT EXISTS material_document_versions(
 id TEXT PRIMARY KEY, source_key TEXT NOT NULL, stream_key TEXT NOT NULL,
 representation_kind TEXT NOT NULL, identity_kind TEXT NOT NULL, content_identity TEXT NOT NULL,
 version INTEGER NOT NULL, previous_id TEXT, anchor_material_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS material_document_stream ON material_document_versions(stream_key,version);
CREATE TABLE IF NOT EXISTS material_representations(
 material_id TEXT PRIMARY KEY, document_version_id TEXT NOT NULL,
 representation_hash TEXT NOT NULL, access_metadata TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS material_document_reads ON material_representations(document_version_id,representation_hash);
"""


def material_text(body):
    return str(body.get("text") or body.get("content") or body.get("claim") or body.get("excerpt") or body.get("summary") or "")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def source_key(body, account_id):
    """Stable source identity; read limits, page ranges and model claims are not sources."""
    url = str(body.get("url") or "")
    try:
        p = urlsplit(url)
        url = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
    except ValueError:
        pass
    owner = (account_id or "") if body.get("scope") != "public" else ""
    return _hash([url or body.get("source_identifier") or _hash(material_text(body)),
                  body.get("scope", "account"), owner])


def representation_kind(body):
    if body.get("representation") == "agent_extraction":
        return "agent_extraction"
    if body.get("access") == "generated_report" or body.get("classification") == "agent_analysis":
        return "generated_analysis"
    if body.get("document_id"):
        return "original_document"
    if body.get("access") in ("search_excerpt", "summary") or body.get("representation") == "summary" or (
            body.get("summary") and not any(body.get(k) for k in ("text", "content", "claim", "excerpt"))):
        return "summary"
    return "text_representation"


def access_metadata(body):
    keys = ("access", "page_range", "pages_read", "start_page", "end_page", "offset", "range", "access_range",
            "requested_range", "returned_range", "extracted_range", "returned_chars", "requested_chars", "truncated", "limit",
            "extraction_method", "extraction_note", "query", "text_length", "page_count", "incomplete_text")
    result = {k: body[k] for k in keys if k in body}
    if body.get("pages"):
        result["pages"] = [{k: v for k, v in p.items() if k != "text"} for p in body["pages"] if isinstance(p, dict)]
    return result


def index_of(body):
    keys = ("id", "classification", "category", "scope", "representation", "symbol", "security_id", "symbols", "url", "source_title", "title",
            "source", "publisher", "unit", "uncertainties", "conflicts", "security_ids", "access_scope", "published_at", "fetched_at", "retrieved_at", "document_id", "final_url", "event_period", "access", "truncated", "error", "page_count", "total_pages",
            "page_range", "pages_read", "start_page", "end_page", "access_range", "range", "requested_range", "returned_range", "extracted_range", "version", "previous_id", "source_id", "changed_at", "content_hash", "change_summary",
            "offset", "returned_chars", "requested_chars", "text_length", "extraction_method", "extraction_note", "document_version_id",
            "document_version", "document_identity_kind", "representation_kind", "read_completeness", "change_kind", "legacy_version", "material_ids", "source_material_ids")
    result = {k: v for k, v in body.items() if k in keys}
    # Metadata authored by a model/source is also untrusted in size.
    for k, v in list(result.items()):
        if isinstance(v, str):
            result[k] = v[:2000]
        elif isinstance(v, (list, dict)) and len(json.dumps(v, default=str)) > 2000:
            result[k] = str(v)[:2000]
    result["summary"] = str(body.get("summary") or material_text(body))[:400]
    result["char_count"] = len(material_text(body))
    if body.get("pages"):
        result["pages"] = [{k: v for k, v in p.items() if k in ("page", "text_start", "text_end", "text_length", "truncated", "source_url")} for p in body["pages"][:100] if isinstance(p, dict)]
    return result


class MaterialStore:
    def migrate_material_catalog(self, db):
        from .store import dumps, loads
        rows = db.execute("""SELECT m.id,m.body,c.account_id FROM materials m
            LEFT JOIN material_catalog c ON c.material_id=m.id
            LEFT JOIN material_representations r ON r.material_id=m.id
            WHERE c.material_id IS NULL OR r.material_id IS NULL ORDER BY m.created_at,m.rowid""").fetchall()
        for row in rows:
            body = loads(row["body"])
            use = db.execute("SELECT account_id FROM material_uses WHERE material_id=? LIMIT 1", (row["id"],)).fetchone()
            account_id = row["account_id"] or (use[0] if use else "")
            if body.get("version") is not None:
                body.setdefault("legacy_version", body["version"])
            for key in ("source_id", "previous_id", "content_hash", "change_summary", "diff"):
                if key in body:
                    body.setdefault("legacy_" + key, body.pop(key))
            body["id"] = row["id"]
            document, _, _ = self._document_version(db, body, account_id)
            self._bind_representation(db, body, document)
            db.execute("UPDATE materials SET body=? WHERE id=?", (dumps(body), row["id"]))
            self._catalog(db, body, account_id)
        # Collapse old per-read notifications onto the original-version anchor.
        # Already delivered aliases stay delivered; a migration never replays them.
        for row in db.execute("""SELECT d.anchor_material_id,n.material_id,n.state,n.created_at,n.notified_at
            FROM material_notifications n JOIN material_representations r ON r.material_id=n.material_id
            JOIN material_document_versions d ON d.id=r.document_version_id
            WHERE n.material_id<>d.anchor_material_id AND n.state<>'superseded'""").fetchall():
            db.execute("INSERT OR IGNORE INTO material_notifications VALUES(?,?,?,?)",
                       (row["anchor_material_id"], row["state"], row["created_at"], row["notified_at"]))
            if row["state"] == "notified":
                db.execute("UPDATE material_notifications SET state='notified',notified_at=? WHERE material_id=?",
                           (row["notified_at"], row["anchor_material_id"]))
            db.execute("UPDATE material_notifications SET state='superseded' WHERE material_id=?", (row["material_id"],))

    def _document_version(self, db, body, account_id):
        from .store import uid
        kind = representation_kind(body)
        raw = kind == "original_document"
        key = source_key(body, account_id)
        stream = [key, kind]
        if kind in ("agent_extraction", "generated_analysis"):
            stream.append(_hash([material_text(body), body.get("classification"), body.get("material_ids")]))
        elif kind == "text_representation":
            # Without original bytes only the observed representation can be versioned.
            stream.append({k: body.get(k) for k in ("access", "requested_range", "page_range", "offset")})
        stream_key = _hash(stream)
        identity = str(body["document_id"]) if raw else _hash(material_text(body))
        previous = db.execute("SELECT * FROM material_document_versions WHERE stream_key=? ORDER BY version DESC LIMIT 1", (stream_key,)).fetchone()
        if previous and previous["content_identity"] == identity:
            return dict(previous), False, None
        if raw and body.get("cache_hit"):
            cached = db.execute("SELECT * FROM material_document_versions WHERE stream_key=? AND content_identity=? ORDER BY version DESC LIMIT 1",
                                (stream_key, identity)).fetchone()
            if cached:
                return dict(cached), False, None
        document = dict(id=uid(), source_key=key, stream_key=stream_key, representation_kind=kind,
                        identity_kind="raw_bytes_sha256" if raw else "representation_sha256", content_identity=identity,
                        version=previous["version"] + 1 if previous else 1, previous_id=previous["id"] if previous else None,
                        anchor_material_id=body["id"], created_at=body.get("changed_at") or now())
        db.execute("INSERT INTO material_document_versions VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(document.values()))
        return document, True, dict(previous) if previous else None

    def _bind_representation(self, db, body, document):
        from .store import dumps
        metadata = access_metadata(body)
        digest = _hash([material_text(body), metadata])
        previous = db.execute("SELECT anchor_material_id FROM material_document_versions WHERE id=?", (document["previous_id"],)).fetchone()
        prefixes = {"original_document": "original_content", "summary": "summary", "text_representation": "representation",
                    "agent_extraction": "agent_extraction", "generated_analysis": "generated_analysis"}
        body.update(source_id=document["source_key"], document_version_id=document["id"], document_version=document["version"],
                    document_identity_kind=document["identity_kind"], representation_kind=document["representation_kind"],
                    version=document["version"], previous_id=previous[0] if previous else None, content_hash=digest,
                    change_kind=prefixes[document["representation_kind"]] + ("_changed" if previous else "_added"))
        body["read_completeness"] = ("partial" if body.get("truncated") or body.get("access") in ("excerpt", "search_excerpt", "metadata", "pdf_search")
                                     else "complete_extracted_text" if body.get("access") == "full_text" and body.get("truncated") is False else "unknown")
        db.execute("INSERT OR REPLACE INTO material_representations VALUES(?,?,?,?)", (body["id"], document["id"], digest, dumps(metadata)))

    def _catalog(self, db, body, account_id):
        from .store import dumps
        index = index_of(body)
        db.execute("INSERT OR REPLACE INTO material_catalog VALUES(?,?,?,?,?,?,?,?,?,?)", (body["id"], body["source_id"], body["version"], body.get("previous_id"), body["content_hash"], account_id or "", body.get("scope", "account"), index["summary"], dumps(index), index["char_count"]))

    def save_material(self, material, job_id=None, account_id=None, notify=False):
        from .store import dumps, loads, uid
        body = {k: v for k, v in material.items() if not k.startswith("_")}
        if representation_kind(body) == "summary":
            body.setdefault("representation", "summary")
        if "access" not in body and "access_scope" in body:
            body["access"] = body["access_scope"]
        if "symbols" not in body and "security_ids" in body:
            body["symbols"] = body["security_ids"]
        if "fetched_at" not in body and "retrieved_at" in body:
            body["fetched_at"] = body["retrieved_at"]
        body.setdefault("classification", body.get("category", "uncertain"))
        body.setdefault("content", material_text(body))
        body.setdefault("source_title", body.get("title"))
        body.setdefault("fetched_at", now())
        body.setdefault("published_at", None)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # References to existing immutable material never create a new revision.
            existing = db.execute("SELECT body FROM materials WHERE id=?", (body.get("id", ""),)).fetchone()
            created = document_created = False
            previous = None
            if existing:
                body = loads(existing[0])
                document = dict(db.execute("SELECT * FROM material_document_versions WHERE id=?", (body["document_version_id"],)).fetchone())
            else:
                body.update(id=uid(), mode=self.mode, changed_at=now())
                document, document_created, previous = self._document_version(db, body, account_id)
                digest = _hash([material_text(body), access_metadata(body)])
                read = db.execute("""SELECT m.body FROM material_representations r JOIN materials m ON m.id=r.material_id
                    WHERE r.document_version_id=? AND r.representation_hash=? LIMIT 1""", (document["id"], digest)).fetchone()
                if read:
                    body = loads(read[0])
                else:
                    created = True
                    self._bind_representation(db, body, document)
                if created and document_created and previous:
                    old = db.execute("SELECT body FROM materials WHERE id=?", (previous["anchor_material_id"],)).fetchone()
                    old_text, new_text = material_text(loads(old[0])), material_text(body)
                    prefix = 0
                    for a, b in zip(old_text, new_text):
                        if a != b:
                            break
                        prefix += 1
                    label = "原始内容版本" if document["representation_kind"] == "original_document" else "摘要表示版本" if document["representation_kind"] == "summary" else "表示版本"
                    body["change_summary"] = f"{label} {previous['version']} → {body['version']}；首次变化字符 {prefix}；长度 {len(old_text)} → {len(new_text)}"
                    body["diff"] = {"first_changed_offset": prefix, "before": old_text[max(0, prefix-100):prefix+1000], "after": new_text[max(0, prefix-100):prefix+1000]}
                if created:
                    fingerprint = _hash([document["id"], digest])
                    db.execute("INSERT INTO materials VALUES(?,?,?,?)", (body["id"], fingerprint, dumps(body), now()))
                    self._catalog(db, body, account_id)
            if notify and body.get("scope") == "public" and document["representation_kind"] not in ("agent_extraction", "generated_analysis") and body.get("access") != "failed":
                # Notification existence is independent of which reader first saved a version.
                # Its anchor is stable even when later reads expose additional pages/characters.
                db.execute("INSERT OR IGNORE INTO material_notifications VALUES(?,'pending',?,NULL)", (document["anchor_material_id"], now()))
            if job_id and account_id:
                db.execute("INSERT OR IGNORE INTO material_uses VALUES(?,?,?)", (body["id"], job_id, account_id))
        return {**body, "_created": bool(created), "_updated": bool(document_created and previous), "_document_created": bool(document_created)}

    def pending_material_notifications(self, limit=100):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT material_id FROM material_notifications WHERE state='pending' ORDER BY created_at,material_id LIMIT ?", (limit,))]

    def complete_material_notifications(self, material_ids):
        with self.connect() as db:
            db.executemany("UPDATE material_notifications SET state='notified',notified_at=? WHERE material_id=?", [(now(), material_id) for material_id in material_ids])

    def material_index(self, account_id=None, query="", limit=30, offset=0, public_only=False):
        from .store import loads
        with self.connect() as db:
            rows = db.execute("""SELECT c.metadata FROM material_catalog c JOIN materials m ON c.material_id=m.id
                 WHERE (?=0 OR c.scope='public') AND (? IS NULL OR c.scope='public' OR c.account_id=?
                 OR EXISTS(SELECT 1 FROM material_uses u WHERE u.material_id=c.material_id AND u.account_id=?))
                 AND (?='' OR instr(lower(m.body),lower(?))>0)
                 ORDER BY m.created_at DESC,m.rowid DESC LIMIT ? OFFSET ?""",
                 (int(public_only), account_id, account_id, account_id, query, query, min(max(int(limit), 1), 100), max(int(offset), 0))).fetchall()
        return [loads(r[0]) for r in rows]

    def material_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM material_catalog WHERE scope='public'").fetchone()[0]

    def material_accessible(self, material_id, account_id):
        with self.connect() as db:
            if not account_id:
                return bool(db.execute("SELECT 1 FROM material_catalog WHERE material_id=? AND scope='public'", (material_id,)).fetchone())
            return bool(db.execute("""SELECT 1 FROM material_catalog c WHERE material_id=? AND (scope='public' OR account_id=?
               OR EXISTS(SELECT 1 FROM material_uses u WHERE u.material_id=c.material_id AND u.account_id=?))""", (material_id, account_id or "", account_id or "")).fetchone())

    def material_range(self, material_id, start=0, length=8000):
        body = self.material(material_id)
        text = material_text(body)
        start, length = max(0, int(start)), min(max(1, int(length)), 12000)
        start = min(start, len(text))
        end = min(len(text), start + length)
        return {**index_of(body), "text": text[start:end], "start": start, "end": end, "total_chars": len(text),
                "next_start": end if end < len(text) else None, "truncated": start > 0 or end < len(text) or bool(body.get("truncated")),
                "source_truncated": bool(body.get("truncated"))}
