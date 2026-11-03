"""SQLite storage: bots, messages, jobs, action items, library and the shared knowledge store (agent/knowledge.py, FTS5)."""
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
import uuid
from pathlib import Path

COLORS = ["#4c9aff", "#3ecf8e", "#f5b84b", "#ff7a90", "#a78bfa", "#2dd4bf"]

SCHEMA = """
create table if not exists bots(id text primary key, name text not null, persona text not null default '', color text, created real, kind text not null default 'bot', phone_id text);
create table if not exists alerts(key text primary key, at real);
create table if not exists launch_facts(phone text not null, package text not null, ok integer not null, at real, note text, primary key(phone, package));
create table if not exists messages(id integer primary key autoincrement, bot_id text not null, role text not null, kind text not null,
  content text not null default '', meta text not null default '{}', created real, updated real);
create index if not exists messages_bot on messages(bot_id, id);
create table if not exists jobs(id text primary key, bot_id text not null, goal text not null, status text not null, started real, ended real,
  result text not null default '', trace_dir text, after text, verify text, routine text, target text, files text, report text);
create table if not exists items(id text primary key, bot_id text not null, job_id text, message_id integer, kind text not null,
  payload text not null default '{}', status text not null default 'open', answer text, created real, answered real);
create table if not exists attachments(id text primary key, bot_id text not null, message_id integer, mime text not null, path text not null, w integer, h integer, bytes integer, created real, name text);
create table if not exists library(id text primary key, bot_id text not null, job_id text, title text not null, path text not null, created real, kind text);
create table if not exists vault(id text primary key, owner text not null, label text not null, kind text not null, nonce blob not null,
  ciphertext blob not null, created real);
create table if not exists vault_audit(id integer primary key autoincrement, secret_id text, label text, job_id text, app text, at real);
create table if not exists routines(id text primary key, bot_id text not null, name text not null, prompt text not null, schedule text not null,
  enabled integer not null default 1, next_run real, last_run real, created real, last_status text);
create table if not exists connectors(id text primary key, kind text not null, name text not null, transport text not null, url text not null default '', command text not null default '',
  auth text not null default '{}', secret_label text, status text not null, tools text not null default '[]', error text not null default '', fails integer not null default 0, created real, updated real);
create virtual table if not exists memory using fts5(text, bot_id unindexed, created unindexed);
"""


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("meta", "payload", "schedule", "verify", "files", "tools", "auth"):
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
        self.mask = lambda text: text  # the app sets an AccountMasker here (agent/privacy.py): phone-derived text hides the phones' account addresses
        with self.lock:
            self.conn.execute("pragma journal_mode=wal")
            self.conn.executescript(SCHEMA)
            if "kind" not in [r["name"] for r in self.conn.execute("pragma table_info(bots)")]:  # databases created before the master bot
                self.conn.execute("alter table bots add column kind text not null default 'bot'")
                self.conn.commit()
            for table, cols in (("jobs", ("after", "verify", "routine", "target", "files", "report")), ("routines", ("last_status",)), ("bots", ("phone_id",)), ("library", ("kind",)), ("attachments", ("name",))):  # databases created before these columns
                have = [r["name"] for r in self.conn.execute(f"pragma table_info({table})")]
                for col in cols:
                    if col not in have:
                        self.conn.execute(f"alter table {table} add column {col} text")
                        self.conn.commit()
        from agent.knowledge import Knowledge  # here, not at the top: knowledge.py is built on this class
        self.knowledge = Knowledge(self)  # what the bots remember and share, and what outside agents may retrieve

    def _q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def _x_count(self, sql: str, args=()) -> int:
        """Run a write and return how many rows it changed."""
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # bots
    def create_bot(self, name: str, persona: str = "", kind: str = "bot", phone_id: str | None = None) -> dict:
        bid = uuid.uuid4().hex[:10]
        color = "#8b7cf6" if kind == "master" else COLORS[int(bid, 16) % len(COLORS)]
        self._x("insert into bots(id,name,persona,color,created,kind,phone_id) values(?,?,?,?,?,?,?)",
                (bid, name.strip()[:60], persona.strip()[:4000], color, time.time(), kind, phone_id))
        return self.get_bot(bid)

    def phone_bot(self, phone_id: str, kind: str = "phone") -> dict | None:
        """The bot of one phone (kind 'phone' for a real phone, 'virtual' for a virtual one such as Cloud phone 2): the only bot whose tasks run on it."""
        return self._one("select * from bots where kind=? and phone_id=? limit 1", (kind, phone_id))

    # what happened when an app was opened on a phone (ok=1 it ran, ok=0 it crashed or would not stay up): the agent uses it to go straight to the phone that works
    def set_launch_fact(self, phone: str, package: str, ok: bool, note: str = "") -> None:
        self._x("insert or replace into launch_facts(phone,package,ok,at,note) values(?,?,?,?,?)", (phone, package, 1 if ok else 0, time.time(), note[:200]))

    def launch_facts(self, package: str | None = None) -> list[dict]:
        if package:
            return self._q("select * from launch_facts where package=? order by at desc", (package,))
        return self._q("select * from launch_facts order by at desc")

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
                "memories": one("select count(*) n from knowledge where visibility='private' and bot_id=?"), "screenshots": one("select count(*) n from library where bot_id=?")}

    def job_ids(self, bot_id: str) -> list[str]:
        return [r["id"] for r in self._q("select id from jobs where bot_id=?", (bot_id,))]

    def delete_bot(self, bot_id: str) -> list[str]:
        """Remove a bot and everything it owns. Returns the library file paths so the caller can delete them."""
        with self.lock:
            paths = [r["path"] for r in self.conn.execute("select path from library where bot_id=?", (bot_id,)).fetchall()]
            paths += [r["path"] for r in self.conn.execute("select path from attachments where bot_id=?", (bot_id,)).fetchall()]
            for table in ("messages", "jobs", "items", "library", "memory", "routines", "attachments"):
                self.conn.execute(f"delete from {table} where bot_id=?", (bot_id,))
            self.conn.execute("delete from bots where id=?", (bot_id,))
            self.conn.commit()
        self.knowledge.delete_for_bot(bot_id)  # its private notes go; what it shared with the other bots stays
        return paths

    # attachments: pictures the owner shared in chat (agent/images.py). An upload belongs to a bot and has no message until the message is sent.
    def add_attachment(self, bot_id: str, mime: str, path: str, w: int, h: int, size: int, aid: str | None = None, name: str | None = None) -> dict:
        aid = aid or uuid.uuid4().hex[:12]
        self._x("insert into attachments(id,bot_id,message_id,mime,path,w,h,bytes,created,name) values(?,?,?,?,?,?,?,?,?,?)", (aid, bot_id, None, mime, path, w, h, size, time.time(), name))
        return self.get_attachment(aid)

    def get_attachment(self, aid: str) -> dict | None:
        return self._one("select * from attachments where id=?", (aid,))

    def unattached(self, bot_id: str, ids: list[str]) -> list[dict]:
        """The uploads among `ids` that belong to this bot and are not on a message yet, in the order asked (an id of another bot, or one already sent, is left out)."""
        rows = {r["id"]: r for r in self._q("select * from attachments where bot_id=? and message_id is null", (bot_id,))}
        return [rows[i] for i in ids if i in rows]

    def unattached_count(self, bot_id: str) -> int:
        return self._one("select count(*) n from attachments where bot_id=? and message_id is null", (bot_id,))["n"]

    def attach(self, ids: list[str], message_id: int) -> None:
        for i in ids:
            self._x("update attachments set message_id=? where id=?", (message_id, i))

    def delete_attachment(self, aid: str) -> None:
        self._x("delete from attachments where id=?", (aid,))

    def stale_attachments(self, max_age_s: float) -> list[dict]:
        """Uploads that were never sent with a message and are older than max_age_s."""
        return self._q("select * from attachments where message_id is null and created<?", (time.time() - max_age_s,))

    def images_today(self) -> int:
        """Pictures made by Muse Image since local midnight (all bots): the daily cap is counted on the chat messages, so it survives a restart."""
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self._one("select count(*) n from messages where kind='image' and created>=?", (midnight,))["n"]

    def attachment_notice_sent(self, bot_id: str) -> bool:
        return bool(self._q("select 1 from messages where bot_id=? and kind='bot_note' and meta like '%image-privacy%' limit 1", (bot_id,)))

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
        if role != "user":
            content = self.mask(content)
        mid = self._x("insert into messages(bot_id,role,kind,content,meta,created,updated) values(?,?,?,?,?,?,?)",
                      (bot_id, role, kind, content, json.dumps(meta or {}), now, now))
        return self.get_message(mid)

    def get_message(self, mid: int) -> dict | None:
        return self._one("select * from messages where id=?", (mid,))

    def update_message(self, mid: int, content: str | None = None, meta: dict | None = None, kind: str | None = None) -> dict:
        cur = self.get_message(mid)
        if content is not None and cur["role"] != "user":
            content = self.mask(content)
        self._x("update messages set content=?, meta=?, kind=?, updated=? where id=?",
                (cur["content"] if content is None else content, json.dumps(cur["meta"] if meta is None else meta),
                 kind or cur["kind"], time.time(), mid))
        return self.get_message(mid)

    def delete_message(self, mid: int) -> None:
        self._x("delete from messages where id=?", (mid,))

    def messages(self, bot_id: str, limit: int = 200) -> list[dict]:
        rows = self._q("select * from messages where bot_id=? order by id desc limit ?", (bot_id, limit))
        return list(reversed(rows))

    def messages_before(self, bot_id: str, before: int, limit: int = 100) -> list[dict]:
        """The page of messages that comes just before message id `before` (oldest first), for scrolling back in a long chat."""
        rows = self._q("select * from messages where bot_id=? and id<? order by id desc limit ?", (bot_id, before, limit))
        return list(reversed(rows))

    # jobs
    def create_job(self, bot_id: str, goal: str, after: str | None = None, verify: dict | None = None, routine: str | None = None, target: str = "",
                   files: list[str] | None = None, report: str | None = None) -> dict:
        """`after` is the id of a job that must finish successfully before this one runs; `verify` is a phone check (agent/checks.py) that must pass for it to count as done."""
        jid = uuid.uuid4().hex[:10]
        self._x("insert into jobs(id,bot_id,goal,status,started,after,verify,routine,target,files,report) values(?,?,?,?,?,?,?,?,?,?,?)",
                (jid, bot_id, goal, "queued", time.time(), after, json.dumps(verify) if verify else None, routine, target or None, json.dumps(files) if files else None, report or None))
        return self.get_job(jid)

    def get_job(self, jid: str) -> dict | None:
        return self._one("select * from jobs where id=?", (jid,))

    def update_job(self, jid: str, **fields) -> dict:
        cols = {"status", "ended", "result", "trace_dir", "goal"}
        sets = {k: v for k, v in fields.items() if k in cols}
        for k in ("result", "goal"):
            if isinstance(sets.get(k), str):
                sets[k] = self.mask(sets[k])
        if sets:
            self._x(f"update jobs set {', '.join(f'{k}=?' for k in sets)} where id=?", (*sets.values(), jid))
        return self.get_job(jid)

    def active_jobs(self, bot_id: str | None = None) -> list[dict]:
        """Queued, running and waiting jobs, oldest first (all bots when `bot_id` is None)."""
        where, args = "status in ('queued','running','waiting')", ()
        if bot_id:
            where, args = where + " and bot_id=?", (bot_id,)
        return self._q(f"select * from jobs where {where} order by started", args)

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
    def add_library(self, bot_id: str, job_id: str | None, title: str, path: str, kind: str = "screenshot") -> dict:
        lid = uuid.uuid4().hex[:10]
        self._x("insert into library(id,bot_id,job_id,title,path,created,kind) values(?,?,?,?,?,?,?)", (lid, bot_id, job_id, self.mask(title)[:160], path, time.time(), kind))
        return self.get_library(lid)

    def get_library(self, lid: str) -> dict | None:
        return self._one("select * from library where id=?", (lid,))

    def library(self, bot_id: str) -> list[dict]:
        return self._q("select * from library where bot_id=? order by created desc", (bot_id,))

    # memory: a bot's own private notes, kept in the knowledge store (agent/knowledge.py)
    def memory_add(self, bot_id: str, text: str) -> None:
        self.knowledge.add(text.strip()[:2000], kind="memory", visibility="private", bot_id=bot_id)

    def memory_search(self, bot_id: str, query: str, limit: int = 5) -> list[str]:
        return [h["text"] for h in self.knowledge.search(query, viewer=("bot", bot_id), visibilities=("private",), limit=limit)]

    def memory_recent(self, bot_id: str, limit: int = 3) -> list[str]:
        return [h["text"] for h in self.knowledge.recent(("bot", bot_id), kinds=("memory",), visibilities=("private",), limit=limit)]

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
        cols = {"enabled", "next_run", "last_run", "last_status"}
        sets = {k: v for k, v in fields.items() if k in cols}
        if sets:
            self._x(f"update routines set {', '.join(f'{k}=?' for k in sets)} where id=?", (*sets.values(), rid))
        return self.get_routine(rid)

    def routine_delete(self, rid: str) -> bool:
        with self.lock:
            n = self.conn.execute("delete from routines where id=?", (rid,)).rowcount
            self.conn.commit()
        return n > 0

    def routine_busy(self, rid: str) -> bool:
        """Is a task started by this routine still queued, running or waiting?"""
        return bool(self._q("select 1 from jobs where routine=? and status in ('queued','running','waiting') limit 1", (rid,)))

    def routines_due(self, now: float) -> list[dict]:
        return self._q("select * from routines where enabled=1 and next_run<=?", (now,))
