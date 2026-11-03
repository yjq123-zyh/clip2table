"""Rebuildable SQLite indexes for immutable dataset objects and ledger views."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class DiskSet:
    def __enter__(self):
        self.db = sqlite3.connect("")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("CREATE TABLE seen(key TEXT PRIMARY KEY)")
        return self

    def __contains__(self, key):
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def add(self, key):
        self.db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (key,))

    def __exit__(self, *exc):
        self.db.close()


@contextmanager
def database(root):
    root = Path(root)
    path = root / "views" / "index.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-4096")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id));
        CREATE INDEX IF NOT EXISTS capture_run ON objects(kind,json_extract(body,'$.run_id'),json_extract(body,'$.page'));
        CREATE TABLE IF NOT EXISTS parse_rows(parse_id TEXT,n INTEGER,body TEXT,PRIMARY KEY(parse_id,n));
        CREATE TABLE IF NOT EXISTS members(capture_id TEXT PRIMARY KEY,parse_id TEXT,n INTEGER);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS observations(view_id TEXT,capture_id TEXT,n INTEGER,parse_id TEXT,key TEXT,
          PRIMARY KEY(view_id,capture_id,n));
        CREATE INDEX IF NOT EXISTS observation_key ON observations(view_id,key);
        CREATE TABLE IF NOT EXISTS entities(view_id TEXT,key TEXT,label TEXT,body TEXT,PRIMARY KEY(view_id,key));
        CREATE INDEX IF NOT EXISTS entity_label ON entities(view_id,label);
        CREATE TABLE IF NOT EXISTS view_state(id TEXT PRIMARY KEY,revision TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS changes(view_id TEXT,n INTEGER,body TEXT,PRIMARY KEY(view_id,n));
        CREATE TABLE IF NOT EXISTS pending_keys(view_id TEXT,pending_id TEXT,key TEXT,PRIMARY KEY(view_id,pending_id));
        CREATE INDEX IF NOT EXISTS pending_entity ON pending_keys(view_id,key);
        """)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            from .errors import InputError
            raise InputError(f"不支持索引格式 {version}", code="unsupported_version")
        initialized = db.execute("SELECT value FROM settings WHERE key='initialized'").fetchone()
        if not initialized:
            for kind, directory in (("capture", "captures"), ("parse", "parses"), ("revision", "revisions")):
                base = root / directory
                paths = base.glob("*/meta.json") if kind != "revision" else base.glob("*.json")
                for file in paths:
                    body = json.loads(file.read_text(encoding="utf-8"))
                    identity = body.get(kind + "_id")
                    if identity:
                        db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (kind, identity, dumps(body)))
                        if kind == "parse":
                            _parse_rows(db, identity, file.parent / "rows.jsonl")
            db.execute("INSERT OR REPLACE INTO settings VALUES('initialized','1')")
            db.execute("PRAGMA user_version=1")
        _events(db, root)
        db.commit()
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _parse_rows(db, identity, path):
    with path.open(encoding="utf-8") as stream:
        db.executemany("INSERT OR REPLACE INTO parse_rows VALUES(?,?,?)",
                       ((identity, n, line) for n, line in enumerate(stream) if line.strip()))


def record(root, kind, identity, body):
    from . import revisions as R
    events = Path(root) / "events"
    reference = events / "refs" / f"{kind}-{identity}.json"
    if not reference.exists():
        state = R._load(events / "state.json", {"commit_seq": 0})
        sequence = state["commit_seq"] + 1
        while (events / f"{sequence:012}.json").exists():
            sequence += 1
        R._dump({"commit_seq": sequence, "kind": kind, "id": identity,
                 **({"body": body} if kind == "selection" else {})}, events / f"{sequence:012}.json")
        R._dump({"commit_seq": sequence}, events / "state.json")
        R._dump({"commit_seq": sequence}, reference)
    with database(root) as db:
        existing = db.execute("SELECT 1 FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
        db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (kind, identity, dumps(body)))
        if kind == "parse" and not existing:
            _parse_rows(db, identity, Path(root) / "parses" / identity / "rows.jsonl")


def _events(db, root):
    from . import revisions as R
    saved = db.execute("SELECT value FROM settings WHERE key='commit_seq'").fetchone()
    sequence = int(saved[0]) if saved else 0
    while True:
        event = R._load(Path(root) / "events" / f"{sequence + 1:012}.json", None)
        if event is None:
            break
        kind, identity = event["kind"], event["id"]
        if not db.execute("SELECT 1 FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone():
            if kind == "selection":
                body = event.get("body") or {"generation": int(identity)}
            else:
                path = Path(root) / ("revisions" if kind == "revision" else kind + "s")
                body = R._load(path / (identity + ".json") if kind == "revision" else path / identity / "meta.json", None)
            if body is None:
                from .errors import InputError
                raise InputError(f"事件引用的对象缺失：{kind}/{identity}", code="corrupt_metadata")
            db.execute("INSERT INTO objects VALUES(?,?,?)", (kind, identity, dumps(body)))
            if kind == "parse":
                _parse_rows(db, identity, Path(root) / "parses" / identity / "rows.jsonl")
        sequence += 1
    if saved is None or int(saved[0]) != sequence:
        db.execute("INSERT OR REPLACE INTO settings VALUES('commit_seq',?)", (str(sequence),))


def metadata(root, kind, identity):
    with database(root) as db:
        row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
    return json.loads(row[0]) if row else None


def run_captures(root, run_id):
    with database(root) as db:
        return [json.loads(row[0]) for row in db.execute("""SELECT body FROM objects WHERE kind='capture'
            AND json_extract(body,'$.run_id')=? ORDER BY json_extract(body,'$.page')""", (run_id,))]


def _members(db, root, revision):
    head = db.execute("SELECT value FROM settings WHERE key='members'").fetchone()
    if head and head[0] == revision:
        return
    chain = []
    cursor = revision
    while cursor and (not head or head[0] != cursor):
        row = db.execute("SELECT body FROM objects WHERE kind='revision' AND id=?", (cursor,)).fetchone()
        if row is None:
            from .errors import InputError
            raise InputError(f"版本索引缺失：{cursor}", code="corrupt_metadata")
        body = json.loads(row[0])
        chain.append(body)
        if "entries" in body:
            db.execute("DELETE FROM members")
            break
        cursor = body.get("parent")
    else:
        if not cursor:
            db.execute("DELETE FROM members")
    for body in reversed(chain):
        if "entries" in body:
            db.execute("DELETE FROM members")
            entries = body["entries"]
        else:
            entries = body.get("delta", [])
        n = db.execute("SELECT COALESCE(MAX(n),-1)+1 FROM members").fetchone()[0]
        for item in entries:
            db.execute("""INSERT INTO members VALUES(?,?,?) ON CONFLICT(capture_id)
                        DO UPDATE SET parse_id=excluded.parse_id,n=excluded.n""",
                       (item["capture_id"], item["parse_id"], n))
            n += 1
    db.execute("INSERT OR REPLACE INTO settings VALUES('members',?)", (revision,))


class Entries(Sequence):
    def __init__(self, root, revision, count):
        self.root, self.revision, self.count = Path(root), revision, count

    def __len__(self):
        return self.count

    def __iter__(self):
        with database(self.root) as db:
            _members(db, self.root, self.revision)
            for cid, pid in db.execute("SELECT capture_id,parse_id FROM members ORDER BY n"):
                yield {"capture_id": cid, "parse_id": pid}

    def __getitem__(self, key):
        return list(self)[key]

    def __eq__(self, other):
        return list(self) == list(other)


class Overlay(Sequence):
    def __init__(self, base, updates):
        self.base, self.updates = base, list(updates)

    def __iter__(self):
        covered = {cid for cid, _ in self.updates}
        for item in self.base:
            if item["capture_id"] not in covered:
                yield item["capture_id"], item["parse_id"]
        yield from self.updates

    def __len__(self):
        return sum(1 for _ in self)

    def __getitem__(self, key):
        return list(self)[key]


def parse_result(root, parse_id):
    from . import revisions as R
    from .models import ParseResult
    with database(root) as db:
        row = db.execute("SELECT body FROM objects WHERE kind='parse' AND id=?", (parse_id,)).fetchone()
        meta = json.loads(row[0])
        rows = [R._row_from(json.loads(row[0])) for row in db.execute(
            "SELECT body FROM parse_rows WHERE parse_id=? ORDER BY n", (parse_id,))]
    return R.ParseRef.from_dict(meta), ParseResult(
        rows=rows, fields=meta["fields"], coverage=meta.get("coverage", {}),
        total_lines=meta["total_lines"], consumed_lines=meta["consumed_lines"],
        level=meta["level"], notes=meta.get("notes", []), deviations=meta.get("deviations", []))


def rebuild(root):
    from . import revisions as R
    with R.writer(root):
        _rebuild(root)


def _rebuild(root):
    path = Path(root) / "views" / "index.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    with database(root):
        pass


def _object(db, kind, identity):
    row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
    return json.loads(row[0])


def prepare_view(root, revision, policy, decisions=""):
    import hashlib
    from . import revisions as R
    identity = hashlib.sha256(dumps([policy.as_dict(), ""]).encode()).hexdigest()
    with R.writer(root):
        with database(root) as db:
            row = db.execute("SELECT revision FROM view_state WHERE id=?", (identity,)).fetchone()
            head = row[0] if row else None
            cursor, chain = revision, []
            while cursor and cursor != head:
                chain.append(cursor)
                cursor = _object(db, "revision", cursor).get("parent")
        result = None
        for rid in reversed(chain):
            result = _prepare_view(root, rid, policy, decisions)
        return result or _prepare_view(root, revision, policy, decisions)


def _prepare_view(root, revision, policy, decisions=""):
    """Recompute affected entities using the shared ledger reducer."""
    import hashlib
    from . import ledger as L, revisions as R
    view = hashlib.sha256(dumps([policy.as_dict(), ""]).encode()).hexdigest()
    with R.writer(root), database(root) as db:
        existing = db.execute("SELECT revision,body FROM view_state WHERE id=?", (view,)).fetchone()
        prior_state = json.loads(existing[1]) if existing else {}
        decision_change = existing and prior_state.get("decisions", "") != decisions
        if existing and existing[0] == revision and not decision_change:
            return view, json.loads(existing[1])
        rev = _object(db, "revision", revision)
        same_revision = existing and existing[0] == revision
        incremental = existing and (existing[0] == rev.get("parent") or same_revision)
        fields = json.loads(existing[1])["fields"] if incremental else []
        prior = json.loads(existing[1])["counts"] if incremental else {}
        counts = {"dataset_rows": prior.get("dataset_rows", 0), "pending": prior.get("pending", 0)}
        db.execute("CREATE TEMP TABLE affected(key TEXT PRIMARY KEY)")
        if not incremental:
            for table in ("observations", "entities", "changes", "pending_keys"):
                db.execute(f"DELETE FROM {table} WHERE view_id=?", (view,))
            _members(db, root, revision)
            updates = [{"capture_id": c, "parse_id": p} for c, p in db.execute("SELECT capture_id,parse_id FROM members ORDER BY n")]
        else:
            updates = [] if same_revision else rev.get("delta", rev.get("entries", []))
        if decision_change:
            before_decisions = R._load(Path(root) / "decisions" / f"{prior_state.get('decisions', '')}.json", {})
            after_decisions = R._load(Path(root) / "decisions" / f"{decisions}.json", {})
            for pending_id in before_decisions.keys() | after_decisions.keys():
                if before_decisions.get(pending_id) != after_decisions.get(pending_id):
                    db.execute("INSERT OR IGNORE INTO affected SELECT key FROM pending_keys WHERE view_id=? AND pending_id=?", (view, pending_id))
        db.execute("DELETE FROM changes WHERE view_id=?", (view,))
        for entry in updates:
            cid, pid = entry["capture_id"], entry["parse_id"]
            db.execute("INSERT OR IGNORE INTO affected SELECT key FROM observations WHERE view_id=? AND capture_id=?", (view, cid))
            db.execute("DELETE FROM observations WHERE view_id=? AND capture_id=?", (view, cid))
            meta = _object(db, "parse", pid)
            for name in meta["fields"]:
                if name not in fields:
                    fields.append(name)
            for n, body in db.execute("SELECT n,body FROM parse_rows WHERE parse_id=? ORDER BY n", (pid,)):
                row = R._row_from(json.loads(body))
                try:
                    key, _ = policy.key_of(row)
                except (KeyError, ValueError):
                    key = f"@missing:{cid}:{n}"
                db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (view, cid, n, pid, key))
                db.execute("INSERT OR IGNORE INTO affected VALUES(?)", (key,))
        change_count = 0
        for (key,) in db.execute("SELECT key FROM affected ORDER BY key"):
            previous = db.execute("SELECT body FROM entities WHERE view_id=? AND key=?", (view, key)).fetchone()
            before = L.Ledger(policy=policy, revision_id=rev.get("parent") or "")
            if previous:
                old = json.loads(previous[0])
                counts["dataset_rows"] -= len(old["current"])
                counts["pending"] -= len(old["pending"])
                for item in json.loads(previous[0])["chosen"]:
                    version = L.EntityVersion(**item["source"])
                    before.current.append(L.CurrentEntity(item["label"], item["values"], version,
                                                          item["basis"], item["pending"], item["alternatives"]))
            versions = L.VersionSet(policy=policy)
            for cid, pid, raw in db.execute("""SELECT o.capture_id,o.parse_id,r.body FROM observations o
                JOIN parse_rows r ON r.parse_id=o.parse_id AND r.n=o.n WHERE o.view_id=? AND o.key=?""", (view, key)):
                row = R._row_from(json.loads(raw))
                cap = _object(db, "capture", cid)
                pref = _object(db, "parse", pid)
                try:
                    identity, label = policy.key_of(row)
                except (KeyError, ValueError) as error:
                    versions.skipped.append({"capture_id": cid, "parse_id": pid, "row": row.source_line,
                        "values": row.values, "why": f"第 {row.source_line} 行主键缺失或类型不符：{error.args[0]}"})
                    continue
                value, text = L._sort_value(row, policy, cap["observed_at"])
                versions.versions.append(L.EntityVersion(identity, label,
                    {k: "" if row.get(k) is None else str(row.get(k)) for k in pref["fields"]},
                    value, text, cid, pid, pref["rule_id"], cap["page"], cap["observed_at"], row))
            led = L.build(root, revision, policy, decision_set_id=decisions, _versions=versions)
            led.changes = L._changes_against(root, rev.get("parent") or "", led, policy, _before=before)
            current, _ = led.current_rows()
            history, _ = led.history_rows()
            pending, _ = led.pending_rows()
            changes, _ = led.change_rows()
            counts["dataset_rows"] += len(current)
            counts["pending"] += len(pending)
            payload = {"current": current, "history": history, "pending": pending,
                       "chosen": [c.as_dict() for c in led.current]}
            db.execute("DELETE FROM pending_keys WHERE view_id=? AND key=?", (view, key))
            db.executemany("INSERT OR REPLACE INTO pending_keys VALUES(?,?,?)",
                           ((view, pending_id, key) for pending_id in led.candidate_keys))
            label = led.current[0].label if led.current else key
            db.execute("INSERT OR REPLACE INTO entities VALUES(?,?,?,?)", (view, key, label, dumps(payload)))
            for change in changes:
                db.execute("INSERT INTO changes VALUES(?,?,?)", (view, change_count, dumps(change)))
                change_count += 1
        counts.update(new=0, updated=0)
        for kind, count in db.execute("""SELECT json_extract(body,'$.类型'),COUNT(DISTINCT json_extract(body,'$.实体'))
            FROM changes WHERE view_id=? GROUP BY json_extract(body,'$.类型')""", (view,)):
            if kind in counts:
                counts[kind] = count
        changed = db.execute("SELECT COUNT(DISTINCT json_extract(body,'$.实体')) FROM changes WHERE view_id=?", (view,)).fetchone()[0]
        counts["unchanged"] = max(0, counts["dataset_rows"] - changed) if incremental else 0
        state = {"fields": fields, "counts": counts, "baseline": rev.get("parent") or "",
                 "decisions": decisions, "changes_cached": not same_revision}
        db.execute("INSERT OR REPLACE INTO view_state VALUES(?,?,?)", (view, revision, dumps(state)))
        return view, state


def view_rows(root, view_id, view, *, limit=None, offset=0):
    with database(root) as db:
        if view == "changes":
            sql = "SELECT body FROM changes WHERE view_id=? ORDER BY n"
        else:
            sql = "SELECT value FROM entities,json_each(entities.body,?) WHERE view_id=? ORDER BY label"
        params = (view_id,) if view == "changes" else ("$." + view, view_id)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += (limit, offset)
        for row in db.execute(sql, params):
            yield json.loads(row[0])


def view_fields(fields, view):
    return {"current": fields + ["_pending"], "history": ["_实体", "_排序值", "_页面", "_观察时间"] + fields,
            "pending": ["实体", "原因", "字段"], "changes": ["类型", "实体", "字段", "之前", "之后", "说明"]}[view]
