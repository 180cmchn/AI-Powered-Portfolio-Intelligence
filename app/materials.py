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
"""


def material_text(body):
    return str(body.get("text") or body.get("content") or body.get("claim") or body.get("excerpt") or body.get("summary") or "")


def source_key(body, account_id):
    url = str(body.get("url") or "")
    try:
        p = urlsplit(url)
        url = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
    except ValueError:
        pass
    # A page range and a search excerpt are different material representations,
    # not revisions of a whole document. Partial reads never overwrite full reads.
    representation = {k: body.get(k) for k in ("access", "page_range", "pages_read", "start_page", "end_page", "offset", "range", "access_range", "requested_range")}
    identity = [url or hashlib.sha256(material_text(body).encode()).hexdigest(), representation,
                body.get("scope", "account"), account_id if body.get("scope") != "public" else "",
                body.get("classification") if body.get("scope") != "public" else "raw"]
    if body.get("representation") == "agent_extraction":
        # Separate extracted claims from a single source are separate materials,
        # not successive corrections to one another.
        identity.append(hashlib.sha256(material_text(body).encode()).hexdigest())
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def index_of(body):
    keys = ("id", "classification", "category", "scope", "representation", "symbol", "security_id", "symbols", "url", "source_title", "title",
            "source", "publisher", "unit", "uncertainties", "conflicts", "security_ids", "access_scope", "published_at", "fetched_at", "retrieved_at", "document_id", "final_url", "event_period", "access", "truncated", "error", "page_count", "total_pages",
            "page_range", "pages_read", "start_page", "end_page", "access_range", "range", "requested_range", "returned_range", "extracted_range", "version", "previous_id", "source_id", "changed_at", "content_hash", "change_summary")
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
        rows = db.execute("SELECT m.id,m.body FROM materials m LEFT JOIN material_catalog c ON c.material_id=m.id WHERE c.material_id IS NULL ORDER BY m.created_at,m.rowid").fetchall()
        for row in rows:
            body = loads(row["body"])
            use = db.execute("SELECT account_id FROM material_uses WHERE material_id=? LIMIT 1", (row["id"],)).fetchone()
            account_id = use[0] if use else ""
            key = source_key(body, account_id)
            previous = db.execute("SELECT material_id,version FROM material_catalog WHERE source_key=? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
            digest = hashlib.sha256((material_text(body) + "\0" + str(body.get("document_id") or "")).encode()).hexdigest()
            body.update(source_id=key, version=previous[1] + 1 if previous else 1, previous_id=previous[0] if previous else None, content_hash=digest)
            db.execute("UPDATE materials SET body=? WHERE id=?", (dumps(body), row["id"]))
            self._catalog(db, body, account_id)

    def _catalog(self, db, body, account_id):
        from .store import dumps
        index = index_of(body)
        db.execute("INSERT INTO material_catalog VALUES(?,?,?,?,?,?,?,?,?,?)", (body["id"], body["source_id"], body["version"], body.get("previous_id"), body["content_hash"], account_id or "", body.get("scope", "account"), index["summary"], dumps(index), index["char_count"]))

    def save_material(self, material, job_id=None, account_id=None, notify=False):
        from .store import dumps, loads, uid
        body = {k: v for k, v in material.items() if not k.startswith("_")}
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
        key = source_key(body, account_id)
        digest = hashlib.sha256((material_text(body) + "\0" + str(body.get("document_id") or "")).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # References to existing immutable material never create a new revision.
            existing = db.execute("SELECT body FROM materials WHERE id=?", (body.get("id", ""),)).fetchone()
            old = db.execute("SELECT m.body FROM material_catalog c JOIN materials m ON m.id=c.material_id WHERE c.source_key=? ORDER BY c.version DESC LIMIT 1", (key,)).fetchone()
            previous = loads(old[0]) if old else None
            created = not existing and (not previous or previous.get("content_hash") != digest)
            if existing:
                body = loads(existing[0])
            elif not created:
                body = previous
            else:
                body.update(id=uid(), mode=self.mode, source_id=key, content_hash=digest,
                            version=previous["version"] + 1 if previous else 1,
                            previous_id=previous["id"] if previous else None, changed_at=now())
                if previous:
                    old_text, new_text = material_text(previous), material_text(body)
                    # Both immutable texts are retained; a bounded common-prefix excerpt
                    # makes changes inspectable without quadratic diffs on huge reports.
                    prefix = 0
                    for a, b in zip(old_text, new_text):
                        if a != b:
                            break
                        prefix += 1
                    body["change_summary"] = f"版本 {previous['version']} → {body['version']}；首次变化字符 {prefix}；长度 {len(old_text)} → {len(new_text)}"
                    body["diff"] = {"first_changed_offset": prefix, "before": old_text[max(0, prefix-100):prefix+1000], "after": new_text[max(0, prefix-100):prefix+1000]}
                # Version identity permits A -> B -> A corrections to survive.
                fingerprint = hashlib.sha256(f"{key}:{body['version']}:{digest}".encode()).hexdigest()
                db.execute("INSERT INTO materials VALUES(?,?,?,?)", (body["id"], fingerprint, dumps(body), now()))
                self._catalog(db, body, account_id)
                if notify:
                    db.execute("INSERT OR IGNORE INTO material_notifications VALUES(?,'pending',?,NULL)", (body["id"], now()))
            if job_id and account_id:
                db.execute("INSERT OR IGNORE INTO material_uses VALUES(?,?,?)", (body["id"], job_id, account_id))
        return {**body, "_created": bool(created), "_updated": bool(created and previous)}

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
        end = min(len(text), start + length)
        return {**index_of(body), "text": text[start:end], "start": start, "end": end, "total_chars": len(text),
                "next_start": end if end < len(text) else None, "truncated": start > 0 or end < len(text) or bool(body.get("truncated")),
                "source_truncated": bool(body.get("truncated"))}
