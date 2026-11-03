"""A2A (Agent2Agent) service core: storage, the two protocol dialects, the Agent Card, and the JSON-RPC dispatcher.

Inside, everything is kept in the 1.0 shapes (ProtoJSON: camelCase, enum names such as TASK_STATE_WORKING, a Part is exactly one of text | raw | url | data).
Requests in the 0.3 dialect (message/send, "kind" tags, lowercase states) are translated on the way in and out; the method name tells which dialect a caller speaks.
Who may call, what they may ask for and what is stored is in docs/a2a.md. The executor (a2a_exec.py) runs the work; the listener (a2a_server.py) is the door.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from agent import logs
from agent.db import Database

VERSION = "0.1.0"
SCOPES = ("ask", "phone", "context-write")
VERSIONS_SUPPORTED = ("0.3", "1.0")
DEFAULT_RATE = 60
DEFAULT_MAX_RUNNING = 5
MAX_TEXT = 64 * 1024  # characters of text in one message
MAX_FILE = 30 * 1024 * 1024  # decoded bytes of one file
MAX_REQUEST_FILES = 40 * 1024 * 1024  # decoded bytes of files in one request
BLOCK_S = 25  # a blocking SendMessage waits this long, then answers with the working task
MAX_PARTS = 32
CONTEXT_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,100}$")

S = "TASK_STATE_"
SUBMITTED, WORKING, COMPLETED, FAILED, CANCELED, INPUT_REQUIRED, REJECTED, AUTH_REQUIRED = (S + n for n in ("SUBMITTED", "WORKING", "COMPLETED", "FAILED", "CANCELED", "INPUT_REQUIRED", "REJECTED", "AUTH_REQUIRED"))
TERMINAL = {COMPLETED, FAILED, CANCELED, REJECTED}
INTERRUPTED = {INPUT_REQUIRED, AUTH_REQUIRED}
STATE_03 = {SUBMITTED: "submitted", WORKING: "working", COMPLETED: "completed", FAILED: "failed", CANCELED: "canceled", INPUT_REQUIRED: "input-required", REJECTED: "rejected", AUTH_REQUIRED: "auth-required"}
STATE_FROM_03 = {v: k for k, v in STATE_03.items()}

ERRORS = {"TaskNotFound": -32001, "TaskNotCancelable": -32002, "PushNotificationNotSupported": -32003, "UnsupportedOperation": -32004, "ContentTypeNotSupported": -32005,
          "InvalidAgentResponse": -32006, "ExtendedAgentCardNotConfigured": -32007, "ExtensionSupportRequired": -32008, "VersionNotSupported": -32009,
          "ParseError": -32700, "InvalidRequest": -32600, "MethodNotFound": -32601, "InvalidParams": -32602, "InternalError": -32603}

# method name -> (operation, dialect)
METHODS = {
    "SendMessage": ("send", "1.0"), "SendStreamingMessage": ("stream", "1.0"), "GetTask": ("get", "1.0"), "ListTasks": ("list", "1.0"), "CancelTask": ("cancel", "1.0"),
    "SubscribeToTask": ("subscribe", "1.0"), "CreateTaskPushNotificationConfig": ("push_set", "1.0"), "GetTaskPushNotificationConfig": ("push_get", "1.0"),
    "ListTaskPushNotificationConfigs": ("push_list", "1.0"), "DeleteTaskPushNotificationConfig": ("push_delete", "1.0"), "GetExtendedAgentCard": ("ext_card", "1.0"),
    "message/send": ("send", "0.3"), "message/stream": ("stream", "0.3"), "tasks/get": ("get", "0.3"), "tasks/cancel": ("cancel", "0.3"), "tasks/resubscribe": ("subscribe", "0.3"),
    "tasks/pushNotificationConfig/set": ("push_set", "0.3"), "tasks/pushNotificationConfig/get": ("push_get", "0.3"), "tasks/pushNotificationConfig/list": ("push_list", "0.3"),
    "tasks/pushNotificationConfig/delete": ("push_delete", "0.3"), "agent/getAuthenticatedExtendedCard": ("ext_card", "0.3"),
}
ASKING = {"retrieve-context": "ask", "ask": "ask", "store-context": "context-write", "phone-task": "phone", "link-check": "phone", "apk-check": "phone", "show-me": "phone"}


class A2AError(Exception):
    def __init__(self, name: str, message: str, data=None):
        super().__init__(message)
        self.name, self.code, self.message, self.data = name, ERRORS[name], message, data

    def rpc(self) -> dict:
        e = {"code": self.code, "message": self.message}
        if self.data is not None:
            e["data"] = self.data
        return e


class AuthError(Exception):
    """The token is missing, wrong or revoked (HTTP 401), or the caller is going too fast (HTTP 429)."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def _d(s, default=None):
    try:
        return json.loads(s) if isinstance(s, str) and s else (default if default is not None else {})
    except ValueError:
        return default if default is not None else {}


SCHEMA = """
create table if not exists a2a_clients(id text primary key, name text not null, token_hash text not null, scopes text not null, rate_per_min integer not null, max_running integer not null,
  bot_id text, created real, last_seen real, revoked real);
create table if not exists a2a_tasks(id text primary key, context_id text not null, client_id text not null, bot_id text, skill text not null default '', state text not null,
  status_message text, metadata text not null default '{}', jobs text not null default '[]', created real, updated real, finished real, error text not null default '');
create index if not exists a2a_tasks_client on a2a_tasks(client_id, created);
create index if not exists a2a_tasks_context on a2a_tasks(client_id, context_id);
create table if not exists a2a_messages(id integer primary key autoincrement, task_id text not null, context_id text not null, message_id text not null, role text not null, parts text not null,
  metadata text not null default '{}', created real, unique(task_id, message_id));
create table if not exists a2a_artifacts(id integer primary key autoincrement, task_id text not null, artifact_id text not null, name text not null default '', description text not null default '',
  parts text not null, metadata text not null default '{}', created real, unique(task_id, artifact_id));
create table if not exists a2a_files(id text primary key, task_id text, client_id text not null, filename text not null, media_type text not null, path text not null, bytes integer not null, created real);
create table if not exists a2a_events(seq integer primary key autoincrement, task_id text not null, event text not null, created real);
create index if not exists a2a_events_task on a2a_events(task_id, seq);
create table if not exists a2a_push(id text primary key, task_id text not null, client_id text not null, url text not null, token text not null default '', auth_scheme text not null default '',
  auth_credentials text not null default '', dialect text not null default '1.0', created real);
create table if not exists a2a_audit(id integer primary key autoincrement, client_id text, method text, task_id text, ok integer, detail text, created real);
create table if not exists a2a_config(key text primary key, value text not null);
"""


def ensure_schema(db: Database) -> None:
    with db.lock:
        db.conn.executescript(SCHEMA)
        db.conn.commit()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def scrub(text: str) -> str:
    """The owner knows the phones as Cloud phone, Cloud phone 2 and your phone: an outside agent never hears the internal names."""
    text = re.sub(r"\s*\(\w*(?:Error|Exception)\)", "", text)  # "(RuntimeError)" and the like: what went wrong inside is the owner's business
    text = re.sub(r"\bgames[ -]phone\b", "Cloud phone 2", text, flags=re.I)
    text = re.sub(r"\bredroid\b", "Cloud phone 2", text, flags=re.I)
    return re.sub(r"\bemulator\b", "Cloud phone", text, flags=re.I)


class Store:
    """Everything the A2A service keeps, in the database."""

    def __init__(self, db: Database):
        self.db = db
        ensure_schema(db)

    # config
    def config(self, key: str, default: str = "") -> str:
        r = self.db._one("select value from a2a_config where key=?", (key,))
        return r["value"] if r else default

    def set_config(self, key: str, value: str) -> None:
        self.db._x("insert into a2a_config(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, value))

    # clients
    @staticmethod
    def _client(r: dict | None) -> dict | None:
        if r is None:
            return None
        r = dict(r)
        r["scopes"] = _d(r["scopes"], [])
        r.pop("token_hash", None)
        return r

    def create_client(self, name: str, scopes: list[str], rate_per_min: int = DEFAULT_RATE, max_running: int = DEFAULT_MAX_RUNNING, bot_id: str | None = None) -> tuple[dict, str]:
        name = " ".join(str(name).split())[:40]
        if not name:
            raise ValueError("a name is required")
        scopes = [s for s in dict.fromkeys(scopes) if s in SCOPES]
        if not scopes:
            raise ValueError("choose at least one permission: " + ", ".join(SCOPES))
        token = "a2a_" + secrets.token_urlsafe(32)
        cid = uuid.uuid4().hex[:10]
        self.db._x("insert into a2a_clients(id,name,token_hash,scopes,rate_per_min,max_running,bot_id,created) values(?,?,?,?,?,?,?,?)",
                   (cid, name, hash_token(token), _j(scopes), max(1, min(int(rate_per_min), 600)), max(1, min(int(max_running), 20)), bot_id, time.time()))
        return self.get_client(cid), token

    def get_client(self, cid: str) -> dict | None:
        return self._client(self.db._one("select * from a2a_clients where id=?", (cid,)))

    def find_client(self, token: str) -> dict | None:
        h = hash_token(token)
        r = self.db._one("select * from a2a_clients where token_hash=? and revoked is null", (h,))
        return self._client(r) if r and hmac.compare_digest(r["token_hash"], h) else None

    def clients(self) -> list[dict]:
        return [self._client(r) for r in self.db._q("select * from a2a_clients order by created")]

    def set_client_bot(self, cid: str, bot_id: str) -> None:
        self.db._x("update a2a_clients set bot_id=? where id=?", (bot_id, cid))

    def rotate_token(self, cid: str) -> str | None:
        if not self.get_client(cid):
            return None
        token = "a2a_" + secrets.token_urlsafe(32)
        self.db._x("update a2a_clients set token_hash=?, revoked=null where id=?", (hash_token(token), cid))
        return token

    def purge_client(self, cid: str) -> None:
        """Delete a caller and everything stored for it (tasks, messages, artifacts, events, files' records, push configs). The files on disk are removed by the caller."""
        ids = [r["id"] for r in self.db._q("select id from a2a_tasks where client_id=?", (cid,))]
        for t in ids:
            for table in ("a2a_messages", "a2a_artifacts", "a2a_events", "a2a_push"):
                self.db._x(f"delete from {table} where task_id=?", (t,))
        for table in ("a2a_tasks", "a2a_files", "a2a_push", "a2a_audit"):
            self.db._x(f"delete from {table} where client_id=?", (cid,))
        self.db._x("delete from a2a_clients where id=?", (cid,))

    def revoke_client(self, cid: str) -> bool:
        if not self.get_client(cid):
            return False
        self.db._x("update a2a_clients set revoked=?, token_hash=? where id=?", (time.time(), hash_token(secrets.token_hex(16)), cid))  # the old token can never match again
        return True

    def touch(self, cid: str) -> None:
        self.db._x("update a2a_clients set last_seen=? where id=?", (time.time(), cid))

    # tasks
    @staticmethod
    def _task(r: dict | None) -> dict | None:
        if r is None:
            return None
        r = dict(r)
        r["status_message"] = _d(r["status_message"], None) or None
        r["metadata"], r["jobs"] = _d(r["metadata"], {}), _d(r["jobs"], [])
        return r

    def create_task(self, client_id: str, context_id: str, bot_id: str | None, skill: str, metadata: dict | None = None) -> dict:
        tid = str(uuid.uuid4())
        now = time.time()
        self.db._x("insert into a2a_tasks(id,context_id,client_id,bot_id,skill,state,metadata,created,updated) values(?,?,?,?,?,?,?,?,?)",
                   (tid, context_id, client_id, bot_id, skill, SUBMITTED, _j(metadata or {}), now, now))
        return self.get_task(tid)

    def get_task(self, tid: str) -> dict | None:
        return self._task(self.db._one("select * from a2a_tasks where id=?", (tid,)))

    def update_task(self, tid: str, **fields) -> dict | None:
        cols = {"state", "status_message", "jobs", "finished", "error", "updated", "metadata", "skill"}
        sets = {k: (_j(v) if k in ("status_message", "jobs", "metadata") else v) for k, v in fields.items() if k in cols}
        sets.setdefault("updated", time.time())
        self.db._x(f"update a2a_tasks set {', '.join(f'{k}=?' for k in sets)} where id=?", (*sets.values(), tid))
        return self.get_task(tid)

    def tasks(self, client_id: str | None = None, context_id: str | None = None, state: str | None = None, after: float | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where, args = ["1=1"], []
        for col, val, op in (("client_id", client_id, "="), ("context_id", context_id, "="), ("state", state, "="), ("updated", after, ">")):
            if val is not None and val != "":
                where.append(f"{col}{op}?")
                args.append(val)
        total = self.db._one(f"select count(*) n from a2a_tasks where {' and '.join(where)}", tuple(args))["n"]
        rows = self.db._q(f"select * from a2a_tasks where {' and '.join(where)} order by created desc limit ? offset ?", (*args, limit, offset))
        return [self._task(r) for r in rows], total

    def running(self, client_id: str) -> int:
        return self.db._one("select count(*) n from a2a_tasks where client_id=? and state in (?,?,?)", (client_id, SUBMITTED, WORKING, INPUT_REQUIRED))["n"]

    def context_known(self, client_id: str, context_id: str) -> bool:
        return bool(self.db._one("select 1 from a2a_tasks where client_id=? and context_id=? limit 1", (client_id, context_id)))

    # messages, artifacts
    def add_message(self, task_id: str, context_id: str, message_id: str, role: str, parts: list, metadata: dict | None = None) -> dict | None:
        try:
            self.db._x("insert into a2a_messages(task_id,context_id,message_id,role,parts,metadata,created) values(?,?,?,?,?,?,?)",
                       (task_id, context_id, message_id, role, _j(parts), _j(metadata or {}), time.time()))
        except Exception:  # unique(task_id, message_id): the same message again
            return None
        return self.db._one("select * from a2a_messages where task_id=? and message_id=?", (task_id, message_id))

    def messages(self, task_id: str) -> list[dict]:
        return self.db._q("select * from a2a_messages where task_id=? order by id", (task_id,))

    def find_message(self, client_id: str, message_id: str) -> dict | None:
        """The message a caller already sent (same messageId): sending it again must not run the work again."""
        return self.db._one("select m.* from a2a_messages m join a2a_tasks t on t.id=m.task_id where t.client_id=? and m.message_id=? and m.role='ROLE_USER' limit 1", (client_id, message_id))

    def upsert_artifact(self, task_id: str, artifact_id: str, name: str, description: str, parts: list, metadata: dict | None = None, append: bool = False) -> dict:
        old = self.db._one("select * from a2a_artifacts where task_id=? and artifact_id=?", (task_id, artifact_id))
        if old and append:
            parts = _d(old["parts"], []) + parts
        if old:
            self.db._x("update a2a_artifacts set name=?, description=?, parts=?, metadata=? where id=?", (name or old["name"], description or old["description"], _j(parts), _j(metadata or _d(old["metadata"])), old["id"]))
        else:
            self.db._x("insert into a2a_artifacts(task_id,artifact_id,name,description,parts,metadata,created) values(?,?,?,?,?,?,?)", (task_id, artifact_id, name, description, _j(parts), _j(metadata or {}), time.time()))
        return self.db._one("select * from a2a_artifacts where task_id=? and artifact_id=?", (task_id, artifact_id))

    def artifacts(self, task_id: str) -> list[dict]:
        return self.db._q("select * from a2a_artifacts where task_id=? order by id", (task_id,))

    # events (the stream and its replay)
    def add_event(self, task_id: str, event: dict) -> int:
        return self.db._x("insert into a2a_events(task_id,event,created) values(?,?,?)", (task_id, _j(event), time.time()))

    def events_after(self, task_id: str, seq: int, limit: int = 200) -> list[dict]:
        return [{"seq": r["seq"], "event": _d(r["event"])} for r in self.db._q("select * from a2a_events where task_id=? and seq>? order by seq limit ?", (task_id, seq, limit))]

    def last_seq(self, task_id: str) -> int:
        return self.db._one("select coalesce(max(seq),0) n from a2a_events where task_id=?", (task_id,))["n"]

    # files
    def add_file(self, client_id: str, task_id: str | None, filename: str, media_type: str, path: str, size: int, fid: str | None = None) -> dict:
        fid = fid or uuid.uuid4().hex[:16]
        self.db._x("insert into a2a_files(id,task_id,client_id,filename,media_type,path,bytes,created) values(?,?,?,?,?,?,?,?)", (fid, task_id, client_id, filename[:120], media_type[:100], path, size, time.time()))
        return self.get_file(fid)

    def get_file(self, fid: str) -> dict | None:
        return self.db._one("select * from a2a_files where id=?", (fid,))

    # push notification configs
    def push_set(self, task_id: str, client_id: str, cfg: dict, dialect: str) -> dict:
        pid = str(cfg.get("id") or uuid.uuid4().hex[:12])[:64]
        auth = cfg.get("authentication") if isinstance(cfg.get("authentication"), dict) else {}
        scheme = auth.get("scheme") or (auth.get("schemes") or [""])[0] if auth else ""
        self.db._x("insert or replace into a2a_push(id,task_id,client_id,url,token,auth_scheme,auth_credentials,dialect,created) values(?,?,?,?,?,?,?,?,?)",
                   (pid, task_id, client_id, str(cfg["url"]), str(cfg.get("token") or ""), str(scheme or ""), str(auth.get("credentials") or ""), dialect, time.time()))
        return self.push_get(task_id, client_id, pid)

    def push_get(self, task_id: str, client_id: str, pid: str) -> dict | None:
        return self.db._one("select * from a2a_push where task_id=? and client_id=? and id=?", (task_id, client_id, pid))

    def push_for_task(self, task_id: str) -> list[dict]:
        return self.db._q("select * from a2a_push where task_id=? order by created", (task_id,))

    def push_list(self, task_id: str, client_id: str) -> list[dict]:
        return self.db._q("select * from a2a_push where task_id=? and client_id=? order by created", (task_id, client_id))

    def push_delete(self, task_id: str, client_id: str, pid: str) -> None:
        self.db._x("delete from a2a_push where task_id=? and client_id=? and id=?", (task_id, client_id, pid))

    # audit (never a token or file bytes)
    def audit(self, client_id: str | None, method: str, task_id: str | None, ok: bool, detail: str = "") -> None:
        self.db._x("insert into a2a_audit(client_id,method,task_id,ok,detail,created) values(?,?,?,?,?,?)", (client_id, method[:60], task_id, int(ok), detail[:200], time.time()))
        logs.event("agent.a2a", f"{method} {'ok' if ok else 'refused'}", logging.INFO if ok else logging.WARNING, client=client_id, task=task_id, method=method[:60], ok=bool(ok), detail=detail[:200])

    def audit_rows(self, client_id: str | None = None, limit: int = 50) -> list[dict]:
        if client_id:
            return self.db._q("select * from a2a_audit where client_id=? order by id desc limit ?", (client_id, limit))
        return self.db._q("select * from a2a_audit order by id desc limit ?", (limit,))


# ---- dialects -----------------------------------------------------------------------------------------------------------------------

def part_to_03(p: dict) -> dict:
    meta = p.get("metadata")
    if "text" in p:
        out = {"kind": "text", "text": p["text"]}
    elif "data" in p:
        out = {"kind": "data", "data": p["data"]}
    else:
        f = {"name": p.get("filename"), "mimeType": p.get("mediaType")}
        f["bytes" if "raw" in p else "uri"] = p.get("raw") if "raw" in p else p.get("url")
        out = {"kind": "file", "file": {k: v for k, v in f.items() if v is not None}}
    if meta:
        out["metadata"] = meta
    return out


def part_from_any(p) -> dict:
    """A part from a caller, in either dialect, as a canonical 1.0 part. Raises InvalidParams when it is not exactly one kind of content."""
    if not isinstance(p, dict):
        raise A2AError("InvalidParams", "a part must be an object")
    kind = p.get("kind")
    if kind == "text" or (kind is None and "text" in p):
        out = {"text": p.get("text")}
    elif kind == "data" or (kind is None and "data" in p):
        out = {"data": p.get("data")}
    elif kind == "file" or (kind is None and ("raw" in p or "url" in p)):
        f = p.get("file") if isinstance(p.get("file"), dict) else p
        raw = f.get("bytes", f.get("raw"))
        uri = f.get("uri", f.get("url"))
        out = {"raw": raw} if raw is not None else {"url": uri}
        if f.get("name", f.get("filename")):
            out["filename"] = str(f.get("name", f.get("filename")))[:200]
        if f.get("mimeType", f.get("mediaType")):
            out["mediaType"] = str(f.get("mimeType", f.get("mediaType")))[:100]
    else:
        raise A2AError("InvalidParams", "a part needs text, data, raw or url")
    if not isinstance(next(iter(out.values())), (str, dict, list, int, float, bool)) or next(iter(out.values())) is None:
        raise A2AError("InvalidParams", "a part has no content")
    if "text" in out and not isinstance(out["text"], str):
        raise A2AError("InvalidParams", "text must be a string")
    if isinstance(p.get("metadata"), dict):
        out["metadata"] = p["metadata"]
    return out


def message_out(m: dict, v: str) -> dict:
    """A stored message row (or a canonical message dict) in the caller's dialect."""
    msg = {"messageId": m["message_id"] if "message_id" in m else m["messageId"], "role": m["role"], "parts": _d(m["parts"], []) if isinstance(m["parts"], str) else m["parts"]}
    cid, tid = m.get("context_id", m.get("contextId")), m.get("task_id", m.get("taskId"))
    if cid:
        msg["contextId"] = cid
    if tid:
        msg["taskId"] = tid
    meta = _d(m["metadata"]) if isinstance(m.get("metadata"), str) else m.get("metadata")
    if meta:
        msg["metadata"] = meta
    if v == "0.3":
        msg = {"kind": "message", **msg, "role": "user" if msg["role"] == "ROLE_USER" else "agent", "parts": [part_to_03(p) for p in msg["parts"]]}
    return msg


def artifact_out(a: dict, v: str) -> dict:
    parts = _d(a["parts"], []) if isinstance(a["parts"], str) else a["parts"]
    out = {"artifactId": a.get("artifact_id", a.get("artifactId")), "parts": [part_to_03(p) for p in parts] if v == "0.3" else parts}
    if a.get("name"):
        out["name"] = a["name"]
    if a.get("description"):
        out["description"] = a["description"]
    meta = _d(a["metadata"]) if isinstance(a.get("metadata"), str) else a.get("metadata")
    if meta:
        out["metadata"] = meta
    return out


def status_out(state: str, message: dict | None, v: str, ts: float | None = None) -> dict:
    s = {"state": STATE_03[state] if v == "0.3" else state, "timestamp": now_iso() if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}
    if message:
        s["message"] = message_out(message, v)
    return s


def accepts(modes: list[str] | None, part: dict) -> bool:
    """Does the caller's acceptedOutputModes allow this part? Text is text/plain, data is application/json, a file has its own media type; a trailing /* matches a family."""
    if not modes:
        return True
    mt = "text/plain" if "text" in part else "application/json" if "data" in part else str(part.get("mediaType") or "application/octet-stream")
    return any(m == mt or m == "*/*" or (m.endswith("/*") and mt.startswith(m[:-1])) for m in modes)


# ---- the Agent Card -----------------------------------------------------------------------------------------------------------------

INPUT_MODES = ["text/plain", "application/json", "image/png", "image/jpeg", "image/webp", "application/vnd.android.package-archive", "text/markdown", "text/csv", "text/html"]
OUTPUT_MODES = ["text/plain", "application/json", "image/png", "text/html", "text/markdown"]

SKILLS = [
    {"id": "phone-task", "scope": "phone", "name": "Do something on an Android phone",
     "description": "Hands a goal to a real Android phone in the cloud (Cloud phone, Cloud phone 2, or the owner's own phone when it is connected). The agent opens apps, reads screens, taps and types until the goal is done and checked. "
                    "It asks its owner before it sends, pays, installs or signs in, so such a task can pause in the input-required state until the owner answers on their own device. It never plays games whose publishers ban bot accounts.",
     "tags": ["android", "phone", "automation", "apps"], "examples": ["Open Settings and tell me the Android version", "Install Google Keep and write a note called Dentist", "Which games are installed on Cloud phone 2?"],
     "inputModes": ["text/plain", "application/json", "image/png", "image/jpeg"], "outputModes": ["text/plain", "application/json", "image/png", "text/html", "text/markdown"],
     "outputs": "Artifacts: `answer` (text), `job-results` (data: goal, status and result of each phone task), `screenshot-*` and `picture-*` (image files), `report` and `report-markdown` (files).",
     "schema": {"type": "object", "properties": {"skill": {"const": "phone-task"}, "goal": {"type": "string", "maxLength": 4000, "description": "What to get done, in plain words"},
                                                  "phone": {"enum": ["Cloud phone", "Cloud phone 2"], "description": "Optional: which phone. Default: the agent chooses"},
                                                  "report": {"enum": ["link", "matrix", "guide", "plain"], "description": "Optional: collect pictures into a report"}}, "required": ["goal"]}},
    {"id": "link-check", "scope": "phone", "name": "Is this link safe?",
     "description": "Opens a link in a throwaway browser on a cloud phone, types nothing and taps nothing on the page, and answers with a verdict (looks harmful, looks suspicious, nothing found in these checks), the reasons and what was NOT checked. It never says a link is safe.",
     "tags": ["security", "link", "phishing"], "examples": ["Is https://example.com safe to open?"], "inputModes": ["text/plain", "application/json"], "outputModes": ["application/json", "text/plain", "text/html"],
     "outputs": "Artifacts: `verdict` (data: verdict, reasons), `answer` (text), `report` (file).",
     "schema": {"type": "object", "properties": {"skill": {"const": "link-check"}, "url": {"type": "string", "format": "uri", "pattern": "^https?://"}}, "required": ["url"]}},
    {"id": "apk-check", "scope": "phone", "name": "Check an APK before installing it",
     "description": "Reads an Android app file WITHOUT running it (permissions, signing certificate), installs it on a throwaway cloud phone, launches it for 20 seconds watching for crashes, uninstalls it and returns a verdict and a report. Send the .apk as a file part (raw, base64).",
     "tags": ["security", "apk", "android"], "examples": ["Check this APK"], "inputModes": ["application/vnd.android.package-archive", "text/plain"], "outputModes": ["application/json", "text/html", "text/plain"],
     "outputs": "Artifacts: `verdict` (data), `report` and `report-markdown` (files), `answer` (text).",
     "schema": {"type": "object", "properties": {"skill": {"const": "apk-check"}}, "description": "The APK goes in a file part with filename ending in .apk"}},
    {"id": "show-me", "scope": "phone", "name": "Explain it with a picture",
     "description": "Draws an illustration (Muse Image 1.0) that explains something, or edits a picture you send (add numbered arrows and labels). It is labelled AI-generated. Never for exact facts, real people, or fake documents. There is a daily limit.",
     "tags": ["image", "illustration", "explain"], "examples": ["Show me with a picture what Wi-Fi calling is"], "inputModes": ["text/plain", "application/json", "image/png", "image/jpeg"], "outputModes": ["image/png", "text/plain"],
     "outputs": "Artifacts: `picture` (image file), `answer` (caption text).",
     "schema": {"type": "object", "properties": {"skill": {"const": "show-me"}, "prompt": {"type": "string", "maxLength": 2000}, "caption": {"type": "string", "maxLength": 500}, "size": {"enum": ["square", "wide", "tall"]}}, "required": ["prompt"]}},
    {"id": "ask", "scope": "ask", "name": "Ask the assistant",
     "description": "An answer from the assistant using its shared memory and what it knows. It does not use a phone for this skill.",
     "tags": ["question", "memory"], "examples": ["What did my other agents learn about the Pixel update?", "Summarise what was found about example.com"], "inputModes": ["text/plain", "application/json", "text/markdown", "text/csv"], "outputModes": ["text/plain"],
     "outputs": "Artifact: `answer` (text).",
     "schema": {"type": "object", "properties": {"skill": {"const": "ask"}, "question": {"type": "string", "maxLength": 4000}}, "required": ["question"]}},
    {"id": "retrieve-context", "scope": "ask", "name": "Search the shared knowledge",
     "description": "Searches the knowledge this agent keeps (ranked, with snippets): your own earlier requests and results, and what the owner published to agents. Never other callers' traffic and never the owner's private notes. Answered directly, without a model.",
     "tags": ["retrieval", "search", "context"], "examples": ["flight booking confirmation"], "inputModes": ["text/plain", "application/json"], "outputModes": ["application/json", "text/plain"],
     "outputs": "Artifacts: `search-results` (data: hits with id, title, snippet, kind, taskId, contextId, created, score) and `answer` (text summary).",
     "schema": {"type": "object", "properties": {"skill": {"const": "retrieve-context"}, "query": {"type": "string", "maxLength": 500}, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                                                  "contextId": {"type": "string"}, "taskId": {"type": "string"}, "kind": {"type": "string"}, "since": {"type": "number", "description": "Unix seconds"}}, "required": ["query"]}},
    {"id": "store-context", "scope": "context-write", "name": "Add a note to the shared knowledge",
     "description": "Stores a note that the owner's bots can retrieve later, and that you can retrieve with retrieve-context. Secrets are removed first. Answered directly, without a model.",
     "tags": ["context", "memory", "notes"], "examples": ["The customer prefers delivery after 6 pm"], "inputModes": ["text/plain", "application/json"], "outputModes": ["application/json", "text/plain"],
     "outputs": "Artifacts: `stored` (data: id) and `answer` (text).",
     "schema": {"type": "object", "properties": {"skill": {"const": "store-context"}, "text": {"type": "string", "maxLength": 8000}, "title": {"type": "string", "maxLength": 200}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["text"]}},
]
SKILL_BY_ID = {s["id"]: s for s in SKILLS}


def card(base: str, v: str = "1.0", extended: dict | None = None) -> dict:
    """The Agent Card. `extended` (the caller's own details) is added for GetExtendedAgentCard."""
    skills = [{k: s[k] for k in ("id", "name", "description", "tags", "examples", "inputModes", "outputModes")} for s in SKILLS]
    desc = ("A personal assistant that operates real Android phones in the cloud. Ask it to do things on a phone, check a link or an APK file, explain something with a picture, "
            "answer from its shared memory, or search and add to that memory. It asks its owner before anything is sent, paid, installed or signed in.")
    if extended:
        desc += f" You are connected as '{extended['client']}' with permissions: {', '.join(extended['scopes'])}."
    common = {"name": "Mobile Agent", "description": desc, "version": VERSION, "documentationUrl": base + "/a2a/docs", "defaultInputModes": INPUT_MODES, "defaultOutputModes": OUTPUT_MODES, "skills": skills}
    if extended:
        common["metadata"] = {"client": extended["client"], "scopes": extended["scopes"], "phones": extended["phones"], "limits": extended["limits"]}
    if v == "0.3":
        return {**common, "protocolVersion": "0.3.0", "url": base + "/a2a", "preferredTransport": "JSONRPC", "additionalInterfaces": [{"url": base + "/a2a", "transport": "JSONRPC"}],
                "provider": {"organization": "Mobile Agent (self-hosted)", "url": base}, "capabilities": {"streaming": True, "pushNotifications": True, "stateTransitionHistory": False},
                "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "a2a_...", "description": "A token the owner created for you on the Agents page"}},
                "security": [{"bearer": []}], "supportsAuthenticatedExtendedCard": True}
    return {**common, "supportedInterfaces": [{"url": base + "/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "provider": {"organization": "Mobile Agent (self-hosted)", "url": base}, "capabilities": {"streaming": True, "pushNotifications": True, "extendedAgentCard": True},
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "a2a_...", "description": "A token the owner created for you on the Agents page"}}},
            "securityRequirements": [{"schemes": {"bearer": {"list": []}}}]}


class Limiter:
    """Per-client sliding window (requests a minute) and a per-address lock after repeated bad tokens."""
    def __init__(self, clock=time.time):
        self.clock, self.hits, self.fails, self.locked, self.lock = clock, {}, {}, {}, threading.Lock()

    def allow(self, key: str, per_min: int) -> bool:
        now = self.clock()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= per_min:
                return False
            q.append(now)
            return True

    def blocked(self, addr: str) -> bool:
        with self.lock:
            return self.locked.get(addr, 0) > self.clock()

    def fail(self, addr: str) -> None:
        now = self.clock()
        with self.lock:
            q = self.fails.setdefault(addr, deque())
            q.append(now)
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= 10:
                self.locked[addr] = now + 60
                q.clear()


def b64_size(s: str) -> int:
    return len(s) * 3 // 4 - (s.count("=", -2) if s else 0)
