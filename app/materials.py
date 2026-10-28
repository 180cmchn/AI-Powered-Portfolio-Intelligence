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
CREATE TABLE IF NOT EXISTS material_observations(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 source_key TEXT NOT NULL, started_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_observation_bindings(
 observation_id TEXT NOT NULL, stream_key TEXT NOT NULL, document_version_id TEXT NOT NULL,
 PRIMARY KEY(observation_id,stream_key));
CREATE TABLE IF NOT EXISTS material_stream_heads(
 stream_key TEXT PRIMARY KEY, document_version_id TEXT NOT NULL, observation_sequence INTEGER NOT NULL);
"""


def material_text(body):
    return str(body.get("text") or body.get("content") or body.get("claim") or body.get("excerpt") or body.get("summary") or "")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def parent_material_ids(body):
    """Both historical spellings mean the union of exact, nonempty parent IDs.

    Sorting makes order/duplicates immaterial to identity. Unknown or inaccessible
    IDs remain present so the resolver can report gaps without granting access.
    """
    return sorted({value for key in ("material_ids", "source_material_ids")
                   for value in (body.get(key) if isinstance(body.get(key), list) else [])
                   if isinstance(value, str) and value})


def normalize_parent_references(body):
    if any(key in body for key in ("material_ids", "source_material_ids")):
        parents = parent_material_ids(body)
        body.update(material_ids=parents, source_material_ids=parents)
    return body


def source_key(body, account_id):
    """Stable source identity; read limits, page ranges and model claims are not sources."""
    url = str(body.get("url") or "").strip()
    try:
        p = urlsplit(url)
        url = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
    except ValueError:
        pass
    owner = (account_id or "") if body.get("scope") != "public" or representation_kind(body) in ('agent_extraction', 'generated_analysis') else ""
    return _hash([url or body.get("source_identifier") or _hash(material_text(body)),
                  body.get("scope", "account"), owner])


def representation_kind(body):
    if body.get("representation") == "agent_extraction":
        return "agent_extraction"
    if body.get("access") == "generated_report" or body.get("classification") == "agent_analysis":
        return "generated_analysis"
    if body.get("representation") == "native_search_source":
        return "native_search_source"
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
            "document_version", "document_identity_kind", "representation_kind", "read_completeness", "change_kind", "legacy_version", "material_ids", "source_material_ids",
            "observation_status", "observation_sequence", "observation_started_at", "is_latest_document_observation")
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
    def begin_material_observation(self, url, *, scope="public", account_id=None):
        """Allocate application-owned order before a fetch starts, across restarts.

        This opaque token is passed as a save keyword by the collection layer,
        never read from source/model JSON. Wall clocks are descriptive only.
        """
        from .store import uid
        observation_id = uid()
        with self.connect() as db:
            db.execute("INSERT INTO material_observations(id,source_key,started_at) VALUES(?,?,?)",
                       (observation_id, source_key({"url": url, "scope": scope}, account_id), now()))
        return observation_id

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
            body.pop("_notification_allowed", None)
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

    def _document_version(self, db, body, account_id, observation_id=None):
        from .store import uid
        kind = representation_kind(body)
        raw = kind == "original_document"
        key = source_key(body, account_id)
        stream = [key, kind]
        if kind in ("agent_extraction", "generated_analysis"):
            stream.append(_hash([material_text(body), body.get("classification"), parent_material_ids(body)]))
        elif kind == "text_representation":
            # Without original bytes only the observed representation can be versioned.
            stream.append({k: body.get(k) for k in ("access", "requested_range", "page_range", "offset")})
        stream_key = _hash(stream)
        identity = str(body["document_id"]) if raw else _hash(material_text(body))
        if kind == "native_search_source" and not material_text(body):
            identity = _hash({key: body.get(key) for key in ("url", "source_title", "title", "publisher")})
        if raw:
            return self._ordered_document_version(db, body, key, stream_key, identity, observation_id)
        previous = db.execute("SELECT * FROM material_document_versions WHERE stream_key=? ORDER BY version DESC LIMIT 1", (stream_key,)).fetchone()
        if previous and previous["content_identity"] == identity:
            return dict(previous), False, None
        document = dict(id=uid(), source_key=key, stream_key=stream_key, representation_kind=kind,
                        identity_kind=("visible_excerpt_sha256" if material_text(body) else "source_metadata_sha256")
                            if kind == "native_search_source" else "representation_sha256", content_identity=identity,
                        version=previous["version"] + 1 if previous else 1, previous_id=previous["id"] if previous else None,
                        anchor_material_id=body["id"], created_at=body.get("changed_at") or now())
        db.execute("INSERT INTO material_document_versions VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(document.values()))
        return document, True, dict(previous) if previous else None

    def _ordered_document_version(self, db, body, key, stream_key, identity, observation_id):
        from .store import uid
        observation = db.execute("SELECT * FROM material_observations WHERE id=? AND source_key=?",
                                 (observation_id, key)).fetchone() if observation_id else None
        head = db.execute("""SELECT d.*,h.observation_sequence FROM material_stream_heads h
            JOIN material_document_versions d ON d.id=h.document_version_id WHERE h.stream_key=?""", (stream_key,)).fetchone()
        if head is None:
            # Legacy rows retain their IDs and versions, but cannot establish a
            # trusted collection order retroactively.
            head = db.execute("SELECT *,0 AS observation_sequence FROM material_document_versions WHERE stream_key=? AND version>0 ORDER BY version DESC LIMIT 1", (stream_key,)).fetchone()
        sequence = observation["sequence"] if observation else 0
        late = bool(observation and head and sequence < head["observation_sequence"])
        status = "late_observation" if late else "ordered" if observation else "unverified_order"
        body.update(observation_status=status)
        for name in ("observation_sequence", "observation_started_at", "is_latest_document_observation"):
            body.pop(name, None)
        if observation:
            body.update(observation_sequence=sequence, observation_started_at=observation["started_at"])
        body["_notification_allowed"] = not late
        bound = db.execute("""SELECT d.* FROM material_observation_bindings b JOIN material_document_versions d
            ON d.id=b.document_version_id WHERE b.observation_id=? AND b.stream_key=?""", (observation_id, stream_key)).fetchone() if observation else None
        if bound and bound["content_identity"] != identity:
            # A fetch token identifies exactly one set of original bytes.
            observation, sequence = None, 0
            body["observation_status"] = "unverified_order"
            body.pop("observation_sequence", None)
            body.pop("observation_started_at", None)
            bound = None
        existing = bound
        if existing is None and head and head["content_identity"] == identity:
            existing = head
        if existing is None and (late or not observation):
            existing = db.execute("SELECT * FROM material_document_versions WHERE stream_key=? AND content_identity=? ORDER BY version DESC LIMIT 1", (stream_key, identity)).fetchone()
        advances = bool(observation and not late)
        previous = dict(head) if advances and head and head["observation_sequence"] > 0 and not existing else None
        if existing:
            document = {key: existing[key] for key in ("id", "source_key", "stream_key", "representation_kind", "identity_kind", "content_identity", "version", "previous_id", "anchor_material_id", "created_at")}
            created = False
        else:
            ordered_version = (head["version"] + 1 if head else 1) if advances else 1 if head is None else 0
            document = dict(id=uid(), source_key=key, stream_key=stream_key, representation_kind="original_document",
                            identity_kind="raw_bytes_sha256", content_identity=identity, version=ordered_version,
                            previous_id=previous["id"] if previous else None, anchor_material_id=body["id"], created_at=now())
            db.execute("INSERT INTO material_document_versions VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(document.values()))
            created = True
        if advances or head is None:
            db.execute("INSERT OR REPLACE INTO material_stream_heads VALUES(?,?,?)", (stream_key, document["id"], sequence))
        if observation:
            db.execute("INSERT OR IGNORE INTO material_observation_bindings VALUES(?,?,?)", (observation_id, stream_key, document["id"]))
        if late or (not observation and head and head["id"] != document["id"]):
            body["_notification_allowed"] = False
        return document, created, previous

    def _bind_representation(self, db, body, document):
        from .store import dumps
        metadata = access_metadata(body)
        digest = _hash([material_text(body), metadata])
        previous = db.execute("SELECT anchor_material_id FROM material_document_versions WHERE id=?", (document["previous_id"],)).fetchone()
        prefixes = {"original_document": "original_content", "summary": "summary", "text_representation": "representation",
                    "agent_extraction": "agent_extraction", "generated_analysis": "generated_analysis", "native_search_source": "visible_source"}
        body.update(source_id=document["source_key"], document_version_id=document["id"], document_version=document["version"],
                    document_identity_kind=document["identity_kind"], representation_kind=document["representation_kind"],
                    version=document["version"], previous_id=previous[0] if previous else None, content_hash=digest,
                    change_kind=prefixes[document["representation_kind"]] + ("_changed" if previous else "_added"))
        if document["representation_kind"] == "original_document" and body.get("observation_status") in ("unverified_order", "late_observation"):
            body["change_kind"] = "original_content_observed"
        body["read_completeness"] = ("partial" if body.get("truncated") or body.get("access") in ("excerpt", "search_excerpt", "metadata", "pdf_search")
                                     else "complete_extracted_text" if body.get("access") == "full_text" and body.get("truncated") is False else "unknown")
        if document["representation_kind"] == "native_search_source":
            body["read_completeness"] = "source_link_only" if body.get("access") == "source_link" else "visible_excerpt"
        db.execute("INSERT OR REPLACE INTO material_representations VALUES(?,?,?,?)", (body["id"], document["id"], digest, dumps(metadata)))

    def _catalog(self, db, body, account_id):
        from .store import dumps
        index = index_of(body)
        db.execute("INSERT OR REPLACE INTO material_catalog VALUES(?,?,?,?,?,?,?,?,?,?)", (body["id"], body["source_id"], body["version"], body.get("previous_id"), body["content_hash"], account_id or "", body.get("scope", "account"), index["summary"], dumps(index), index["char_count"]))

    def save_material(self, material, job_id=None, account_id=None, notify=False, *, observation_id=None):
        from .store import dumps, loads, uid
        body = normalize_parent_references({k: v for k, v in material.items() if not k.startswith("_")})
        if representation_kind(body) == "native_search_source":
            # A provider citation is not a local download, even if the model
            # copies a field named document_id into a source description.
            body.pop("document_id", None)
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
            notification_allowed = True
            observation_status = None
            previous = None
            if existing:
                body = loads(existing[0])
                document = dict(db.execute("SELECT * FROM material_document_versions WHERE id=?", (body["document_version_id"],)).fetchone())
                head = db.execute("SELECT document_version_id FROM material_stream_heads WHERE stream_key=?", (document["stream_key"],)).fetchone()
                notification_allowed = not head or head[0] == document["id"]
            else:
                body.update(id=uid(), mode=self.mode, changed_at=now())
                document, document_created, previous = self._document_version(db, body, account_id, observation_id)
                notification_allowed = body.pop("_notification_allowed", True)
                observation_status = body.get("observation_status")
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
            if notify and notification_allowed and body.get("scope") == "public" and document["representation_kind"] not in ("agent_extraction", "generated_analysis") and body.get("access") != "failed":
                # Notification existence is independent of which reader first saved a version.
                # Its anchor is stable even when later reads expose additional pages/characters.
                db.execute("INSERT OR IGNORE INTO material_notifications VALUES(?,'pending',?,NULL)", (document["anchor_material_id"], now()))
            if job_id and account_id:
                db.execute("INSERT OR IGNORE INTO material_uses VALUES(?,?,?)", (body["id"], job_id, account_id))
            latest = db.execute("SELECT document_version_id FROM material_stream_heads WHERE stream_key=?", (document["stream_key"],)).fetchone()
        return {**body, "_created": bool(created), "_updated": bool(document_created and previous), "_document_created": bool(document_created),
                "observation_result_status": observation_status or body.get("observation_status"),
                "is_latest_document_observation": latest[0] == document["id"] if latest else None}

    def pending_material_notifications(self, limit=100):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT material_id FROM material_notifications WHERE state='pending' ORDER BY created_at,material_id LIMIT ?", (limit,))]

    def complete_material_notifications(self, material_ids):
        with self.connect() as db:
            db.executemany("UPDATE material_notifications SET state='notified',notified_at=? WHERE material_id=?", [(now(), material_id) for material_id in material_ids])

    def material_index(self, account_id=None, query="", limit=30, offset=0, public_only=False):
        from .store import loads
        with self.connect() as db:
            rows = db.execute("""SELECT c.metadata,r.document_version_id,h.document_version_id AS head_id
                 FROM material_catalog c JOIN materials m ON c.material_id=m.id
                 LEFT JOIN material_representations r ON r.material_id=m.id
                 LEFT JOIN material_document_versions d ON d.id=r.document_version_id
                 LEFT JOIN material_stream_heads h ON h.stream_key=d.stream_key
                 WHERE (?=0 OR c.scope='public') AND
                 ((c.scope='public' AND d.representation_kind NOT IN ('agent_extraction','generated_analysis'))
                  OR (?<>'' AND c.account_id=?))
                 AND (?='' OR instr(lower(m.body),lower(?))>0)
                 ORDER BY m.created_at DESC,m.rowid DESC LIMIT ? OFFSET ?""",
                 (int(public_only), account_id or '', account_id or '', query, query, min(max(int(limit), 1), 100), max(int(offset), 0))).fetchall()
        return [{**loads(r[0]), "is_latest_document_observation": r["head_id"] == r["document_version_id"] if r["head_id"] else None} for r in rows]

    def material_count(self):
        with self.connect() as db:
            return db.execute("""SELECT count(*) FROM material_catalog c
                JOIN material_representations r ON r.material_id=c.material_id
                JOIN material_document_versions d ON d.id=r.document_version_id
                WHERE c.scope='public' AND d.representation_kind NOT IN ('agent_extraction','generated_analysis')""").fetchone()[0]

    def material_accessible(self, material_id, account_id, db=None):
        if db is None:
            with self.connect() as conn:
                return self.material_accessible(material_id, account_id, conn)
        # A historical material_use is evidence of a past read, never a grant to
        # another account's conclusions. Public generated material is also private
        # to its generating account; global generated reports have no such owner.
        return bool(db.execute("""SELECT 1 FROM material_catalog c
            JOIN material_representations r ON r.material_id=c.material_id
            JOIN material_document_versions d ON d.id=r.document_version_id
            WHERE c.material_id=? AND ((c.scope='public' AND
                (d.representation_kind NOT IN ('agent_extraction','generated_analysis') OR ?=''))
                OR (?<>'' AND c.account_id=?))""", (material_id, account_id or '', account_id or '', account_id or '')).fetchone())

    def material_range(self, material_id, start=0, length=8000):
        body = self.material(material_id)
        text = material_text(body)
        start, length = max(0, int(start)), min(max(1, int(length)), 12000)
        start = min(start, len(text))
        end = min(len(text), start + length)
        return {**index_of(body), "text": text[start:end], "start": start, "end": end, "total_chars": len(text),
                "next_start": end if end < len(text) else None, "truncated": start > 0 or end < len(text) or bool(body.get("truncated")),
                "source_truncated": bool(body.get("truncated"))}
