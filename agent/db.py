"""SQLite storage: bots, messages, jobs, action items, library and searchable memory (FTS5)."""
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

COLORS = ["#4c9aff", "#3ecf8e", "#f5b84b", "#ff7a90", "#a78bfa", "#2dd4bf"]

SCHEMA = """
create table if not exists bots(id text primary key, name text not null, persona text not null default '', color text, created real, kind text not null default 'bot');
create table if not exists alerts(key text primary key, at real);
create table if not exists messages(id integer primary key autoincrement, bot_id text not null, role text not null, kind text not null,
  content text not null default '', meta text not null default '{}', created real, updated real);
create index if not exists messages_bot on messages(bot_id, id);
create table if not exists jobs(id text primary key, bot_id text not null, goal text not null, status text not null, started real, ended real,
  result text not null default '', trace_dir text);
create table if not exists items(id text primary key, bot_id text not null, job_id text, message_id integer, kind text not null,
  payload text not null default '{}', status text not null default 'open', answer text, created real, answered real);
create table if not exists library(id text primary key, bot_id text not null, job_id text, title text not null, path text not null, created real);
create table if not exists vault(id text primary key, owner text not null, label text not null, kind text not null, nonce blob not null,
  ciphertext blob not null, created real);
create table if not exists vault_audit(id integer primary key autoincrement, secret_id text, label text, job_id text, app text, at real);
create table if not exists routines(id text primary key, bot_id text not null, name text not null, prompt text not null, schedule text not null,
  enabled integer not null default 1, next_run real, last_run real, created real);
create virtual table if not exists memory using fts5(text, bot_id unindexed, created unindexed);
"""


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("meta", "payload", "schedule"):
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k] or "{}")
    return d


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR query of quoted words."""
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", text.lower())][:12]
    return " OR ".join(f'"{w}"' for w in words)


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("pragma journal_mode=wal")
            self.conn.executescript(SCHEMA)
            if "kind" not in [r["name"] for r in self.conn.execute("pragma table_info(bots)")]:  # databases created before the master bot
                self.conn.execute("alter table bots add column kind text not null default 'bot'")
                self.conn.commit()

    def _q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # bots
    def create_bot(self, name: str, persona: str = "", kind: str = "bot") -> dict:
        bid = uuid.uuid4().hex[:10]
        color = "#8b7cf6" if kind == "master" else COLORS[int(bid, 16) % len(COLORS)]
        self._x("insert into bots(id,name,persona,color,created,kind) values(?,?,?,?,?,?)", (bid, name.strip()[:60], persona.strip()[:4000], color, time.time(), kind))
        return self.get_bot(bid)

    def master_bot(self) -> dict | None:
        return self._one("select * from bots where kind='master' limit 1")

    def alert_once(self, key: str) -> bool:
        """True the first time a key is seen, so each problem is reported to the owner once."""
        with self.lock:
            cur = self.conn.execute("insert or ignore into alerts(key, at) values(?,?)", (key, time.time()))
            self.conn.commit()
            return cur.rowcount == 1

    def get_bot(self, bot_id: str) -> dict | None:
        return self._one("select * from bots where id=?", (bot_id,))

    def rename_bot(self, bot_id: str, name: str, persona: str | None = None) -> dict | None:
        cur = self.get_bot(bot_id)
        if cur:
            self._x("update bots set name=?, persona=? where id=?",
                    (name.strip()[:60], cur["persona"] if persona is None else persona.strip()[:4000], bot_id))
        return self.get_bot(bot_id)

    def bot_counts(self, bot_id: str) -> dict:
        """What deleting a bot removes, for the confirmation dialog."""
        one = lambda sql: self._one(sql, (bot_id,))["n"]
        return {"messages": one("select count(*) n from messages where bot_id=? and kind in ('text','result')"),
                "tasks": one("select count(*) n from jobs where bot_id=?"), "routines": one("select count(*) n from routines where bot_id=?"),
                "memories": one("select count(*) n from memory where bot_id=?"), "screenshots": one("select count(*) n from library where bot_id=?")}

    def job_ids(self, bot_id: str) -> list[str]:
        return [r["id"] for r in self._q("select id from jobs where bot_id=?", (bot_id,))]

    def delete_bot(self, bot_id: str) -> list[str]:
        """Remove a bot and everything it owns. Returns the library file paths so the caller can delete them."""
        with self.lock:
            paths = [r["path"] for r in self.conn.execute("select path from library where bot_id=?", (bot_id,)).fetchall()]
            for table in ("messages", "jobs", "items", "library", "memory", "routines"):
                self.conn.execute(f"delete from {table} where bot_id=?", (bot_id,))
            self.conn.execute("delete from bots where id=?", (bot_id,))
            self.conn.commit()
        return paths

    def list_bots(self) -> list[dict]:
        bots = self._q("select * from bots order by created")
        for b in bots:
            last = self._one("select content, kind, created from messages where bot_id=? and kind in ('text','result','action_item','alert') order by id desc limit 1", (b["id"],))
            b["last"] = (last["content"][:90] if last else "")
            b["last_at"] = last["created"] if last else b["created"]
            b["last_id"] = (self._one("select max(id) m from messages where bot_id=?", (b["id"],)) or {}).get("m") or 0
        return sorted(bots, key=lambda b: (b["kind"] != "master", -b["last_at"]))  # the master bot stays on top

    # messages
    def add_message(self, bot_id: str, role: str, kind: str, content: str = "", meta: dict | None = None) -> dict:
        now = time.time()
        mid = self._x("insert into messages(bot_id,role,kind,content,meta,created,updated) values(?,?,?,?,?,?,?)",
                      (bot_id, role, kind, content, json.dumps(meta or {}), now, now))
        return self.get_message(mid)

    def get_message(self, mid: int) -> dict | None:
        return self._one("select * from messages where id=?", (mid,))

    def update_message(self, mid: int, content: str | None = None, meta: dict | None = None, kind: str | None = None) -> dict:
        cur = self.get_message(mid)
        self._x("update messages set content=?, meta=?, kind=?, updated=? where id=?",
                (cur["content"] if content is None else content, json.dumps(cur["meta"] if meta is None else meta),
                 kind or cur["kind"], time.time(), mid))
        return self.get_message(mid)

    def delete_message(self, mid: int) -> None:
        self._x("delete from messages where id=?", (mid,))

    def messages(self, bot_id: str, limit: int = 200) -> list[dict]:
        rows = self._q("select * from messages where bot_id=? order by id desc limit ?", (bot_id, limit))
        return list(reversed(rows))

    # jobs
    def create_job(self, bot_id: str, goal: str) -> dict:
        jid = uuid.uuid4().hex[:10]
        self._x("insert into jobs(id,bot_id,goal,status,started) values(?,?,?,?,?)", (jid, bot_id, goal, "queued", time.time()))
        return self.get_job(jid)

    def get_job(self, jid: str) -> dict | None:
        return self._one("select * from jobs where id=?", (jid,))

    def update_job(self, jid: str, **fields) -> dict:
        cols = {"status", "ended", "result", "trace_dir"}
        sets = {k: v for k, v in fields.items() if k in cols}
        if sets:
            self._x(f"update jobs set {', '.join(f'{k}=?' for k in sets)} where id=?", (*sets.values(), jid))
        return self.get_job(jid)

    def jobs(self, bot_id: str, limit: int = 20) -> list[dict]:
        return self._q("select * from jobs where bot_id=? order by started desc limit ?", (bot_id, limit))

    # action items
    def create_item(self, bot_id: str, job_id: str | None, kind: str, payload: dict) -> dict:
        iid = uuid.uuid4().hex[:10]
        self._x("insert into items(id,bot_id,job_id,kind,payload,created) values(?,?,?,?,?,?)",
                (iid, bot_id, job_id, kind, json.dumps(payload), time.time()))
        return self.get_item(iid)

    def get_item(self, iid: str) -> dict | None:
        return self._one("select * from items where id=?", (iid,))

    def set_item_message(self, iid: str, message_id: int) -> None:
        self._x("update items set message_id=? where id=?", (message_id, iid))

    def close_item(self, iid: str, status: str, answer: str | None = None) -> dict:
        self._x("update items set status=?, answer=?, answered=? where id=?", (status, answer, time.time(), iid))
        return self.get_item(iid)

    def open_items(self, bot_id: str | None = None) -> list[dict]:
        if bot_id:
            return self._q("select * from items where status='open' and bot_id=?", (bot_id,))
        return self._q("select * from items where status='open'")

    # library
    def add_library(self, bot_id: str, job_id: str | None, title: str, path: str) -> dict:
        lid = uuid.uuid4().hex[:10]
        self._x("insert into library values(?,?,?,?,?,?)", (lid, bot_id, job_id, title[:160], path, time.time()))
        return self.get_library(lid)

    def get_library(self, lid: str) -> dict | None:
        return self._one("select * from library where id=?", (lid,))

    def library(self, bot_id: str) -> list[dict]:
        return self._q("select * from library where bot_id=? order by created desc", (bot_id,))

    # memory (full-text search, no embeddings)
    def memory_add(self, bot_id: str, text: str) -> None:
        self._x("insert into memory(text, bot_id, created) values(?,?,?)", (text.strip()[:2000], bot_id, time.time()))

    def memory_search(self, bot_id: str, query: str, limit: int = 5) -> list[str]:
        q = fts_query(query)
        if not q:
            return []
        rows = self._q("select text from memory where memory match ? and bot_id=? order by bm25(memory) limit ?", (q, bot_id, limit))
        return [r["text"] for r in rows]

    # routines
    def routine_add(self, bot_id: str, name: str, prompt: str, schedule: dict, next_run: float) -> dict:
        rid = uuid.uuid4().hex[:10]
        self._x("insert into routines(id,bot_id,name,prompt,schedule,enabled,next_run,created) values(?,?,?,?,?,1,?,?)",
                (rid, bot_id, name, prompt, json.dumps(schedule), next_run, time.time()))
        return self.get_routine(rid)

    def get_routine(self, rid: str) -> dict | None:
        return self._one("select * from routines where id=?", (rid,))

    def routines(self, bot_id: str) -> list[dict]:
        return self._q("select * from routines where bot_id=? order by created", (bot_id,))

    def routine_update(self, rid: str, **fields) -> dict | None:
        cols = {"enabled", "next_run", "last_run"}
        sets = {k: v for k, v in fields.items() if k in cols}
        if sets:
            self._x(f"update routines set {', '.join(f'{k}=?' for k in sets)} where id=?", (*sets.values(), rid))
        return self.get_routine(rid)

    def routine_delete(self, rid: str) -> bool:
        with self.lock:
            n = self.conn.execute("delete from routines where id=?", (rid,)).rowcount
            self.conn.commit()
        return n > 0

    def routines_due(self, now: float) -> list[dict]:
        return self._q("select * from routines where enabled=1 and next_run<=?", (now,))
