"""The shared knowledge store: what the bots, their finished tasks, their reports and the A2A callers know, in one searchable place.

One SQLite table plus an FTS5 index (porter stemming, so "booking" finds "booked"). Every entry has a visibility and the rules are applied in SQL, before any ranking:
  private  one bot (`bot_id`)          bots    every bot of the owner          agents  the bots and the A2A clients (what the owner published)
A bot sees its own private entries plus every `bots` and `agents` entry. A client (an outside agent) sees the entries it created (`client_id`) plus `agents` entries, nothing else.
The owner (the page) sees everything. See docs/a2a.md. No embeddings: the ranking is BM25 plus a recency boost, a phrase bonus and an all-terms bonus.
"""
import hashlib
import json
import math
import re
import time

from agent import sensitive

VISIBILITIES = ("private", "bots", "agents")
MAX_TEXT = 8000
MAX_TITLE = 200
MAX_QUERY_TOKENS = 16
CANDIDATES = 60  # rows pulled from the index before the second ranking
HALF_LIFE_DAYS = 30.0
RECENCY_WEIGHT = 0.3  # a brand-new entry scores up to 30% more than an old one with the same text match
ALL_TERMS_BONUS = 2.0
PHRASE_BONUS = 1.35
NEAR_DUPLICATE = 0.9  # token overlap above which two hits count as the same note

SCHEMA = """
create table if not exists knowledge(id integer primary key autoincrement, text text not null, title text not null default '', kind text not null default 'note',
  visibility text not null, bot_id text, client_id text, context_id text not null default '', task_id text not null default '', source text not null default '',
  tags text not null default '', meta text not null default '{}', created real not null, updated real not null, hash text not null);
create index if not exists knowledge_vis on knowledge(visibility, bot_id);
create index if not exists knowledge_client on knowledge(client_id);
create index if not exists knowledge_context on knowledge(context_id);
create index if not exists knowledge_task on knowledge(task_id);
create index if not exists knowledge_hash on knowledge(hash);
create index if not exists knowledge_created on knowledge(created);
create virtual table if not exists knowledge_fts using fts5(title, text, tags, tokenize='porter unicode61 remove_diacritics 2', prefix='2 3');
create table if not exists knowledge_meta(key text primary key, value text);
"""

# Words that carry no meaning in a question. Short words that can be the point ("AC", "tv", "x", "5g") are NOT here.
STOPWORDS = frozenset("""a an and are as at be been being by for from has have had how i in is it its me my of on or our please that the their them there these they this to
was we were what when where which who whom why will with you your do does did can could would should about any all some tell show find get give into than then so if
""".split())
TOKEN_RE = re.compile(r"[^\W_]+")
# Spellings people use for one word. Applied to what is indexed and to every query, so "Wi-Fi", "wi fi" and "wifi" are the same word.
CANON = ((re.compile(r"\bwi[\s\-]?fi\b", re.I), "wifi"), (re.compile(r"\be[\s\-]mail", re.I), "email"), (re.compile(r"\bset[\s\-]up\b", re.I), "setup"),
         (re.compile(r"\blog[\s\-]in\b", re.I), "login"), (re.compile(r"\bsign[\s\-]in\b", re.I), "signin"), (re.compile(r"\bback[\s\-]up\b", re.I), "backup"))


class Hit(dict):
    """A search result: a plain dict (JSON friendly) that also answers to attribute access (hit.title)."""
    __getattr__ = dict.get


def canon(text: str) -> str:
    for rx, word in CANON:
        text = rx.sub(word, text or "")
    return text


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(canon(text).lower())


def normalize(text: str) -> str:
    return " ".join(tokens(text))


def snippet(text: str, terms: list[str], width: int = 200) -> str:
    """About `width` characters of the text around the first place a query word starts (plain text, no markup)."""
    flat = " ".join(text.split())
    low = canon(flat).lower()
    at = min((i for i in (low.find(t[:4]) for t in terms if t) if i >= 0), default=0)
    start = max(0, at - width // 4)
    if start:
        start = flat.rfind(" ", 0, start) + 1  # begin on a word
    cut = flat[start:start + width]
    return ("… " if start else "") + cut + (" …" if start + width < len(flat) else "")


def _hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode()).hexdigest()


def query_terms(query) -> tuple[list[str], list[str]]:
    """(all tokens, the meaningful ones) of a free-text query: letters and digits only, so nothing in it can be read as FTS5 syntax."""
    if not isinstance(query, str):
        return [], []
    allt = [t[:40] for t in tokens(query[:2000])][:MAX_QUERY_TOKENS * 2]
    seen, keep = set(), []
    for t in allt:
        if t not in STOPWORDS and t not in seen:
            seen.add(t)
            keep.append(t)
    return allt, keep[:MAX_QUERY_TOKENS]


def _viewer_sql(viewer) -> tuple[str, list]:
    """The visibility rule for a viewer, as SQL on the table alias k."""
    kind, who = (viewer[0], viewer[1] if len(viewer) > 1 else None) if isinstance(viewer, (tuple, list)) and viewer else (None, None)
    if kind == "owner":
        return "1=1", []
    if kind == "bot" and who:
        return "((k.visibility='private' and k.bot_id=?) or k.visibility in ('bots','agents'))", [str(who)]
    if kind == "client" and who:
        return "(k.client_id=? or k.visibility='agents')", [str(who)]
    raise ValueError("viewer must be ('bot', id), ('client', id) or ('owner', None)")


class Knowledge:
    def __init__(self, db):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()
        self._migrate_memory()

    # ---- writing
    def add(self, text: str, *, kind: str = "note", visibility: str = "private", bot_id: str | None = None, client_id: str | None = None, context_id: str = "",
            task_id: str = "", title: str = "", source: str = "", tags=(), meta: dict | None = None, created: float | None = None) -> int | None:
        """Store one entry and return its id. Secrets are hidden first, text is cut at 8000 characters, empty text returns None. An exact repeat (same text, visibility,
        bot, client and context) is not stored twice: the existing entry's id comes back and its time is refreshed."""
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        if visibility == "private" and not bot_id and not client_id:
            raise ValueError("a private entry needs a bot_id (or a client_id)")
        mask = getattr(self.db, "mask", lambda s: s)
        text = mask(sensitive.redact(str(text or ""))[0]).strip()[:MAX_TEXT]
        if not text:
            return None
        title = mask(sensitive.redact(str(title or ""))[0]).strip()[:MAX_TITLE]
        tag_text = " ".join(dict.fromkeys(t for tag in (tags or ()) for t in tokens(str(tag))))[:300]
        now = time.time()
        h = _hash(text)
        with self.db.lock:
            dup = self.db.conn.execute("select id from knowledge where hash=? and visibility=? and coalesce(bot_id,'')=? and coalesce(client_id,'')=? and context_id=? limit 1",
                                       (h, visibility, bot_id or "", client_id or "", context_id or "")).fetchone()
            if dup:
                self.db.conn.execute("update knowledge set updated=? where id=?", (now, dup["id"]))
                self.db.conn.commit()
                return dup["id"]
            try:
                cur = self.db.conn.execute("insert into knowledge(text,title,kind,visibility,bot_id,client_id,context_id,task_id,source,tags,meta,created,updated,hash) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                           (text, title, str(kind)[:40] or "note", visibility, bot_id, client_id, context_id or "", task_id or "", str(source or "")[:200], tag_text,
                                            json.dumps(meta or {}, default=str)[:4000], created or now, created or now, h))
                self.db.conn.execute("insert into knowledge_fts(rowid,title,text,tags) values(?,?,?,?)", (cur.lastrowid, canon(title), canon(text), tag_text))
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise
            return cur.lastrowid

    def update(self, kid: int, *, text: str | None = None, title: str | None = None, tags=None) -> bool:
        """Change an entry's words; the index follows in the same transaction."""
        with self.db.lock:
            row = self.db.conn.execute("select * from knowledge where id=?", (kid,)).fetchone()
            if not row:
                return False
            mask = getattr(self.db, "mask", lambda s: s)
            new_text = row["text"] if text is None else mask(sensitive.redact(text)[0]).strip()[:MAX_TEXT]
            new_title = row["title"] if title is None else mask(sensitive.redact(title)[0]).strip()[:MAX_TITLE]
            new_tags = row["tags"] if tags is None else " ".join(dict.fromkeys(t for tag in tags for t in tokens(str(tag))))[:300]
            if not new_text:
                return False
            try:
                self.db.conn.execute("update knowledge set text=?, title=?, tags=?, hash=?, updated=? where id=?", (new_text, new_title, new_tags, _hash(new_text), time.time(), kid))
                self.db.conn.execute("delete from knowledge_fts where rowid=?", (kid,))
                self.db.conn.execute("insert into knowledge_fts(rowid,title,text,tags) values(?,?,?,?)", (kid, canon(new_title), canon(new_text), new_tags))
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise
            return True

    def _delete_where(self, where: str, args: tuple) -> int:
        with self.db.lock:
            ids = [r["id"] for r in self.db.conn.execute(f"select id from knowledge where {where}", args).fetchall()]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                self.db.conn.execute(f"delete from knowledge_fts where rowid in ({marks})", chunk)
                self.db.conn.execute(f"delete from knowledge where id in ({marks})", chunk)
            self.db.conn.commit()
            return len(ids)

    def delete(self, kid: int) -> bool:
        return self._delete_where("id=?", (kid,)) > 0

    def delete_for_bot(self, bot_id: str) -> int:
        """A bot was deleted: its private entries go. What it shared stays (the other bots and the callers may rely on it); `bot_id` stays on those as plain text."""
        return self._delete_where("visibility='private' and bot_id=?", (bot_id,))

    def delete_for_client(self, client_id: str) -> int:
        """An A2A client was removed: everything it created goes, whatever its visibility."""
        return self._delete_where("client_id=?", (client_id,))

    def erase_all(self) -> int:
        """The owner wipes everything."""
        return self._delete_where("1=1", ())

    # ---- reading
    @staticmethod
    def _public(row: dict) -> Hit:
        h = Hit({k: row[k] for k in ("id", "title", "text", "kind", "visibility", "bot_id", "client_id", "context_id", "task_id", "source", "created")})
        h["tags"] = row["tags"].split() if row.get("tags") else []
        try:
            h["meta"] = json.loads(row.get("meta") or "{}")
        except ValueError:
            h["meta"] = {}
        return h

    def get(self, kid: int, viewer) -> Hit | None:
        vis, vargs = _viewer_sql(viewer)
        row = self.db._one(f"select * from knowledge k where k.id=? and {vis}", (kid, *vargs))
        return self._public(row) if row else None

    @staticmethod
    def _filters(kinds, context_id, task_id, since, visibilities) -> tuple[str, list]:
        sql, args = "", []
        if kinds:
            ks = [str(k) for k in ([kinds] if isinstance(kinds, str) else kinds)][:20]
            sql += f" and k.kind in ({','.join('?' * len(ks))})"
            args += ks
        if visibilities:
            vs = [v for v in ([visibilities] if isinstance(visibilities, str) else visibilities) if v in VISIBILITIES] or ["-"]  # an unknown name matches nothing
            sql += f" and k.visibility in ({','.join('?' * len(vs))})"
            args += vs
        if context_id:
            sql += " and k.context_id=?"
            args.append(context_id)
        if task_id:
            sql += " and k.task_id=?"
            args.append(task_id)
        if since:
            sql += " and k.created>=?"
            args.append(float(since))
        return sql, args

    def recent(self, viewer, *, kinds=None, context_id=None, task_id=None, since=None, visibilities=None, limit: int = 10) -> list[Hit]:
        vis, vargs = _viewer_sql(viewer)
        fsql, fargs = self._filters(kinds, context_id, task_id, since, visibilities)
        rows = self.db._q(f"select * from knowledge k where {vis}{fsql} order by k.created desc, k.id desc limit ?", (*vargs, *fargs, max(1, min(int(limit), 100))))
        return [self._public(r) for r in rows]

    def search(self, query: str, *, viewer, kinds=None, context_id=None, task_id=None, since=None, visibilities=None, limit: int = 5) -> list[Hit]:
        """The best `limit` entries (1 to 20) the viewer may see for a free-text query. Empty when the query has no meaningful word."""
        limit = max(1, min(int(limit or 5), 20))
        allt, terms = query_terms(query)
        if not terms:
            return []
        vis, vargs = _viewer_sql(viewer)
        fsql, fargs = self._filters(kinds, context_id, task_id, since, visibilities)
        sel = (f"select k.*, bm25(knowledge_fts, 4.0, 1.0, 2.0) as r from knowledge_fts join knowledge k on k.id=knowledge_fts.rowid "
               f"where knowledge_fts match ? and {vis}{fsql} order by r limit ?")
        every = " OR ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in terms)
        rows = self.db._q(sel, (every, *vargs, *fargs, CANDIDATES))
        if not rows:
            return []
        if len(terms) > 1:  # which candidates hold every term (the bonus), asked the same way with the filters applied
            both = " ".join(f'"{t}"' for t in terms)
            everywhere = {r["id"] for r in self.db._q(sel, (both, *vargs, *fargs, CANDIDATES * 2))}
        else:
            everywhere = {r["id"] for r in rows}
        now = time.time()
        phrase_all, phrase_terms = " ".join(allt), " ".join(terms)
        scored = []
        for r in rows:
            rel = -r["r"] + 0.05  # BM25 is ~0 in a tiny store (every word is 'common'): the floor lets the bonuses and recency still order the hits
            age_days = max(0.0, (now - r["created"]) / 86400)
            score = rel * (1 + RECENCY_WEIGHT * math.pow(0.5, age_days / HALF_LIFE_DAYS))
            if r["id"] in everywhere:
                score *= ALL_TERMS_BONUS
            body = normalize(r["title"] + " " + r["text"])
            if len(terms) > 1 and (phrase_all in body or phrase_terms in body):
                score *= PHRASE_BONUS
            scored.append((score, r))
        scored.sort(key=lambda p: (-p[0], -p[1]["created"]))
        out, kept = [], []
        for score, r in scored:
            toks = set(tokens(r["title"] + " " + r["text"])[:80])
            if any(r["hash"] == k["hash"] or (toks and len(toks & kt) / len(toks | kt) >= NEAR_DUPLICATE) for k, kt in kept):
                continue
            kept.append((r, toks))
            h = self._public(r)
            h["score"] = round(score, 4)
            h["snippet"] = snippet(r["text"], terms)
            out.append(h)
            if len(out) == limit:
                break
        return out

    def stats(self) -> dict:
        rows = self.db._q("select kind, visibility, count(*) n from knowledge group by kind, visibility")
        by_kind, by_vis = {}, {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["n"]
            by_vis[r["visibility"]] = by_vis.get(r["visibility"], 0) + r["n"]
        return {"total": sum(by_kind.values()), "by_kind": by_kind, "by_visibility": by_vis}

    # ---- the old per-bot memory table
    def _migrate_memory(self) -> None:
        """Once: copy the rows of the old `memory` table in as private entries of kind 'memory'. The old table is left as it was."""
        with self.db.lock:
            if self.db.conn.execute("select 1 from knowledge_meta where key='memory_migrated'").fetchone():
                return
            rows = self.db.conn.execute("select text, bot_id, created from memory order by rowid").fetchall()
        for r in rows:
            if r["bot_id"] and (r["text"] or "").strip():
                self.add(r["text"], kind="memory", visibility="private", bot_id=r["bot_id"], created=float(r["created"] or time.time()))
        self.db._x("insert or replace into knowledge_meta(key, value) values('memory_migrated', ?)", (str(time.time()),))
