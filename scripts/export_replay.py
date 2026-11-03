#!/usr/bin/env python3
"""Export one recorded chat session to static files that web/replay.html can play (no phone, no model calls).

    .venv/bin/python scripts/export_replay.py <bot_id> --out web/replay/hero-a [--title ...] [--from-msg N --to-msg M]
    .venv/bin/python scripts/export_replay.py --demo --out web/replay/demo        # a deterministic synthetic session

The live database is never opened for writing: it is copied with SQLite's online backup into a temp file first.
Everything that is exported passes through `mask_text` (emails, IP addresses, tokens, pairing codes, file paths, phone numbers, URL query strings and
any `--mask` text). Pictures from the owner's chat become a grey placeholder unless `--public-images` is given. Step screenshots are re-encoded as small JPEGs;
`--redact x0,y0,x1,y1` (fractions of the picture, repeatable) blacks out a region on all of them (status bar, account chip, notification area).
"""
import argparse
import io
import ipaddress
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
VERSION = 1
SHOT_WIDTH, SHOT_QUALITY = 360, 60
MAX_GAP_S = 8.0  # an idle gap longer than this is shortened to this, so a replay never sits still
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
TOKEN_RES = (re.compile(r"\bdev_[A-Za-z0-9_-]{16,}"), re.compile(r"(?i)\bbearer\s+\S{8,}"), re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
             re.compile(r"\b[A-Za-z0-9_-]{32,}\b"))
CODE_RE = re.compile(r"\b[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}\b")  # the pairing code alphabet (no 0/O/1/I/L)
PATH_RE = re.compile(r"(?:/(?:Users|home|private|var|tmp|opt|root|Volumes)/[^\s'\"<>)\]]+)")
URL_QUERY_RE = re.compile(r"(https?://[^\s?#'\"<>]+)\?[^\s'\"<>]*")
NUMBER_RE = re.compile(r"(?<!\w)\+?\d(?:[\s-]?\d){9,14}(?!\w)")


def mask_text(text: str, extra: list[str] | tuple[str, ...] = ()) -> str:
    """Remove what must not be published from one string. Order matters: URLs and paths first, then secrets, then addresses."""
    if not text:
        return text
    out = str(text)
    for needle in sorted((x for x in extra if x), key=len, reverse=True):
        out = re.sub(re.escape(needle), "[hidden]", out, flags=re.I)
    out = URL_QUERY_RE.sub(r"\1?…", out)
    out = PATH_RE.sub("[path]", out)
    out = EMAIL_RE.sub("[email]", out)
    for rx in TOKEN_RES:
        out = rx.sub("[token]", out)
    out = CODE_RE.sub("[code]", out)

    def ip(m: re.Match) -> str:
        try:
            ipaddress.ip_address(m.group(0))
            return "[ip]"
        except ValueError:
            return m.group(0)
    out = IPV4_RE.sub(ip, out)
    out = IPV6_RE.sub(ip, out)  # a clock time such as 12:30:45 is not a valid IPv6 address, so it stays
    out = NUMBER_RE.sub("[number]", out)
    return out


def shrink_jpeg(data: bytes, width: int = SHOT_WIDTH, quality: int = SHOT_QUALITY, redact: list[tuple[float, float, float, float]] = ()) -> tuple[bytes, int, int]:
    """Re-encode a picture as a small JPEG (about `width` px wide) with the `redact` regions (fractions) blacked out. Returns (bytes, w, h)."""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    if redact:
        d = ImageDraw.Draw(img)
        for x0, y0, x1, y1 in redact:
            d.rectangle([round(x0 * img.width), round(y0 * img.height), round(x1 * img.width), round(y1 * img.height)], fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=False, progressive=False)
    return buf.getvalue(), img.width, img.height


def placeholder(w: int, label: str = "picture hidden") -> bytes:
    img = Image.new("RGB", (max(60, min(w or 360, 360)), 200), (58, 63, 76))
    ImageDraw.Draw(img).text((12, 12), label, fill=(190, 196, 210))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=SHOT_QUALITY)
    return buf.getvalue()


def phone_tag(target: str | None) -> str:
    t = (target or "").strip()
    if not t:
        return "emulator"
    if t.startswith("phone:"):
        return "phone"
    if t.startswith("virtual:"):
        return "games" if t.split(":", 1)[1] == "games" else t.split(":", 1)[1]
    return t


def describe_action(action: dict, prompt: str = "", extra=()) -> str:
    """One short caption for a recorded action, in plain words. Element ids are turned into the label the model saw."""
    a = action or {}
    kind = str(a.get("type", "")).lower()
    if kind == "tap":
        if "element" in a:
            m = re.search(rf"^\s*{int(a['element'])}: '(.*?)' at", prompt or "", re.M)
            return mask_text(f"tap “{m.group(1)}”" if m else f"tap element {a['element']}", extra)
        return f"tap ({a.get('x')}, {a.get('y')})"
    if kind in ("swipe", "scroll"):
        return f"{kind} {a.get('direction', '')}".strip()
    if kind == "type":
        return mask_text(f"type “{str(a.get('text', ''))[:60]}”", extra)
    if kind == "type_secret":
        return "type a saved secret (approved by the owner, never shown)"
    if kind == "find_app":
        return mask_text(f"look for “{str(a.get('query', ''))[:40]}”", extra)
    if kind == "key":
        return f"press {a.get('key', '')}"
    if kind == "open_app":
        return mask_text(f"open {a.get('package', '')}", extra)
    if kind == "open_url":
        return mask_text(f"open {a.get('url', '')}", extra)
    if kind == "wait":
        return f"wait {a.get('seconds', '')} s".strip()
    if kind == "done":
        return mask_text("done: " + str(a.get("answer", ""))[:140], extra)
    if kind == "fail":
        return mask_text("stopped: " + str(a.get("reason", ""))[:140], extra)
    if kind in ("ask", "handoff", "need_info", "need_secret", "request_otp"):
        return {"ask": "ask the owner", "handoff": "hand over to the owner", "need_info": "ask for a detail", "need_secret": "ask for a secret",
                "request_otp": "ask for a one-time code"}[kind]
    return mask_text(kind.replace("_", " ") or "step", extra)


def _compress(times: list[float], max_gap: float) -> list[int]:
    """Absolute seconds (sorted ascending) -> ms from zero with every gap capped at max_gap. Equal times stay equal."""
    out, prev_raw, now = [], None, 0.0
    for t in times:
        if prev_raw is not None:
            now += min(max(t - prev_raw, 0.0), max_gap)
        out.append(round(now * 1000))
        prev_raw = t
    return out


def _load_steps(trace: Path | None) -> list[dict]:
    """[{n, thought, action, prompt, png(bytes|None), ms(float seconds the step took before its action ran)}] from a job's trace folder."""
    if not trace or not trace.is_dir():
        return []
    timing = {}
    tf = trace / "timing.jsonl"
    if tf.exists():
        for line in tf.read_text().splitlines():
            try:
                row = json.loads(line)
                timing[int(row["step"])] = row
            except (ValueError, KeyError, TypeError):
                pass
    steps = []
    for jf in sorted(trace.glob("step*.json")):
        m = re.fullmatch(r"step(\d+)\.json", jf.name)
        if not m:
            continue
        n = int(m.group(1))
        try:
            data = json.loads(jf.read_text())
        except ValueError:
            continue
        reply = data.get("reply") or {}
        png = trace / f"step{n:02d}.png"
        row = timing.get(n, {})
        steps.append({"n": n, "thought": str(reply.get("thought", "")), "action": reply.get("action") or {}, "prompt": str(data.get("prompt", "")),
                      "png": png.read_bytes() if png.exists() else None,
                      "pre": (row.get("observe", 0) + row.get("model", 0)) / 1000.0, "total": sum(row.get(k, 0) for k in ("observe", "model", "act", "settle")) / 1000.0})
    return sorted(steps, key=lambda s: s["n"])


def _find_trace(job: dict, db_dir: Path) -> Path | None:
    td = job.get("trace_dir")
    for cand in ([Path(td)] if td else []) + ([ROOT / td] if td else []) + [db_dir / "runs" / job["id"], ROOT / "data" / "runs" / job["id"]]:
        if cand and cand.is_dir():
            return cand
    return None


def build_session(conn: sqlite3.Connection, bot_id: str, db_dir: Path, *, title: str = "", from_msg: int = 0, to_msg: int = 10**12, max_gap: float = MAX_GAP_S,
                  mask: tuple[str, ...] = (), public_images: bool = False, redact=(), name: str = "session", synthetic: bool = False) -> tuple[dict, dict]:
    """Returns (manifest, files) where files maps a relative path like 'img/s-j1-01.jpg' to its bytes."""
    conn.row_factory = sqlite3.Row
    bot = conn.execute("select * from bots where id=?", (bot_id,)).fetchone()
    if not bot:
        raise SystemExit(f"no bot {bot_id!r} in the database")
    msgs = [dict(r) for r in conn.execute("select * from messages where bot_id=? and id between ? and ? and kind != 'status' order by id", (bot_id, from_msg, to_msg))]
    if not msgs:
        raise SystemExit("no messages in that range")
    for m in msgs:
        m["meta"] = json.loads(m.get("meta") or "{}")
    lo, hi = min(m["created"] for m in msgs) - 5, max(m["created"] for m in msgs) + 120
    wanted = {m["meta"].get("job_id") for m in msgs if m["meta"].get("job_id")}
    jobs = [dict(r) for r in conn.execute("select * from jobs where bot_id=? order by started", (bot_id,))]
    jobs = [j for j in jobs if (lo <= (j["started"] or 0) <= hi) or j["id"] in wanted]
    items = {r["id"]: dict(r) for r in conn.execute("select * from items where bot_id=?", (bot_id,))}
    atts = {r["id"]: dict(r) for r in conn.execute("select * from attachments where bot_id=?", (bot_id,))}

    files: dict[str, bytes] = {}
    raw: list[tuple[float, int, dict]] = []  # (absolute time, tie-break order, event)
    order = 0

    def add(t: float, ev: dict) -> None:
        nonlocal order
        raw.append((t, order, ev))
        order += 1

    att_n = 0
    for m in msgs:
        ev = {"type": "message", "id": m["id"], "role": m["role"], "kind": m["kind"], "text": mask_text(m["content"], mask)}
        images = []
        for a in m["meta"].get("attachments") or []:
            att_n += 1
            row = atts.get(a.get("id"))
            src = f"img/att-{att_n:02d}.jpg"
            data = None
            if public_images and row and Path(row["path"]).exists():
                data = shrink_jpeg(Path(row["path"]).read_bytes(), redact=list(redact))[0]
            files[src] = data or placeholder(int(a.get("w") or 360))
            images.append({"src": src, "hidden": not data})
        if images:
            ev["images"] = images
        if m["kind"] == "action_item" and m["meta"].get("item_id") in items:
            it = items[m["meta"]["item_id"]]
            payload = json.loads(it.get("payload") or "{}")
            ev["card"] = {"id": it["id"], "kind": str(payload.get("kind") or it["kind"]), "question": mask_text(str(payload.get("question", m["content"])), mask),
                          "options": [mask_text(str(o), mask) for o in (payload.get("options") or [])][:8], "status": "open"}
            add(m["created"], ev)
            if it.get("status") == "answered" and it.get("answered"):
                ans = it.get("answer") or ""
                add(it["answered"], {"type": "card", "id": it["id"], "status": "answered", "answer": mask_text(str(ans), mask) if it["kind"] not in ("secret", "otp") else "(hidden)"})
            continue
        add(m["created"], ev)

    for j in jobs:
        tag = phone_tag(j.get("target"))
        start = j["started"] or lo
        add(start, {"type": "job", "job": j["id"], "status": "running", "goal": mask_text(j["goal"], mask), "phone": tag})
        steps = _load_steps(_find_trace(j, db_dir))
        cum = 0.0
        for s in steps:
            shot = None
            if s["png"]:
                shot = f"img/{name}-{j['id']}-{s['n']:02d}.jpg"
                files[shot] = shrink_jpeg(s["png"], redact=list(redact))[0]
            add(start + cum + s["pre"], {"type": "step", "job": j["id"], "n": s["n"], "thought": mask_text(s["thought"], mask),
                                          "caption": describe_action(s["action"], s["prompt"], mask), "shot": shot, "phone": tag})
            cum += s["total"] or (s["pre"] + 1.0)
        end = j["ended"] or (start + cum + 1)
        end = max(end, start + cum)
        add(end, {"type": "job", "job": j["id"], "status": j["status"], "result": mask_text(j.get("result") or "", mask), "phone": tag})

    raw.sort(key=lambda x: (x[0], x[1]))
    ms = _compress([t for t, _, _ in raw], max_gap)
    events = []
    for (t, _, ev), tm in zip(raw, ms):
        events.append({"t": tm, **ev})
    first_user = next((e["text"] for e in events if e["type"] == "message" and e["role"] == "user" and e.get("text")), "")
    tags = sorted({e["phone"] for e in events if e["type"] == "job"})
    manifest = {"version": VERSION, "name": name, "title": mask_text(title, mask) or (f"{bot['name']}: {first_user[:60]}" if first_user else bot["name"]),
                "synthetic": synthetic, "bot": {"name": mask_text(bot["name"], mask), "kind": bot["kind"], "color": bot["color"] or "#4c9aff"},
                "phones": tags, "duration_ms": events[-1]["t"] if events else 0,
                "steps": sum(1 for e in events if e["type"] == "step"), "events": events}
    return manifest, files


def write_session(out: Path, manifest: dict, files: dict) -> None:
    """Replace the session folder's contents (session.json and img/) so a re-export never leaves stale pictures."""
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out / "img", ignore_errors=True)
    (out / "img").mkdir()
    for rel, data in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (out / "session.json").write_text(json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    update_index(out.parent)


def update_index(parent: Path) -> None:
    """index.json lists every session folder next to this one (sorted by name), for the landing page."""
    rows = []
    for sj in sorted(parent.glob("*/session.json")):
        try:
            m = json.loads(sj.read_text())
        except ValueError:
            continue
        rows.append({"name": sj.parent.name, "title": m.get("title", sj.parent.name), "duration_ms": m.get("duration_ms", 0), "phones": m.get("phones", []),
                     "steps": m.get("steps", 0), "synthetic": bool(m.get("synthetic"))})
    (parent / "index.json").write_text(json.dumps({"version": VERSION, "sessions": rows}, indent=1, sort_keys=True) + "\n")


# ---- a deterministic synthetic session (no real data) -------------------------------------------------------------------------------------------------------------

DEMO_SCHEMA = """
create table bots(id text primary key, name text, persona text, color text, created real, kind text, phone_id text);
create table messages(id integer primary key autoincrement, bot_id text, role text, kind text, content text, meta text, created real, updated real);
create table jobs(id text primary key, bot_id text, goal text, status text, started real, ended real, result text, trace_dir text, after text, verify text, routine text, target text, files text);
create table items(id text primary key, bot_id text, job_id text, message_id integer, kind text, payload text, status text, answer text, created real, answered real);
create table attachments(id text primary key, bot_id text, message_id integer, mime text, path text, w integer, h integer, bytes integer, created real);
"""


def _screen(title: str, rows: list[str], accent=(70, 110, 200), crash: bool = False) -> bytes:
    """A made-up phone screen (not a real app): status bar, a title, a few rows. Deterministic."""
    img = Image.new("RGB", (720, 1280), (18, 22, 30))
    d = ImageDraw.Draw(img)
    big, mid, small = (ImageFont.load_default(size=n) for n in (56, 42, 30))
    d.rectangle([0, 0, 720, 70], fill=(10, 12, 16))
    d.text((24, 18), "12:00", fill=(200, 205, 215), font=small)
    d.rectangle([0, 70, 720, 210], fill=accent)
    d.text((32, 108), title, fill=(255, 255, 255), font=big)
    for i, r in enumerate(rows):
        y = 250 + i * 160
        d.rounded_rectangle([32, y, 688, y + 120], radius=24, fill=(34, 40, 54))
        d.text((64, y + 36), r, fill=(225, 230, 240), font=mid)
    if crash:
        d.rounded_rectangle([60, 520, 660, 820], radius=30, fill=(48, 52, 64))
        d.text((100, 580), "The app keeps stopping", fill=(255, 255, 255), font=mid)
        d.text((100, 700), "Close app", fill=(150, 190, 255), font=mid)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_demo(dbfile: Path, runs: Path) -> str:
    """Create a synthetic database and trace folders for one made-up story. Returns the bot id."""
    conn = sqlite3.connect(dbfile)
    conn.executescript(DEMO_SCHEMA)
    base, bot = 1_700_000_000.0, "demobot01"
    conn.execute("insert into bots values(?,?,?,?,?,?,?)", (bot, "Demo assistant", "", "#4c9aff", base - 100, "bot", None))
    msgs = [
        (0, "user", "text", "open the sample game and tell me what you see", {}),
        (2, "assistant", "text", "On it. I'll open the sample game and report back.", {}),
        (62, "assistant", "text", "The sample game keeps stopping on Phone (3 crashes), so I moved it to Phone 2 and opened it there. It shows its start screen; I did not play it.", {}),
        (70, "user", "text", "now play it for me", {}),
        (73, "assistant", "text", "I won't play this one for you: automated play can get your account banned. I can keep it open, and you can use Take control to play it yourself.", {}),
        (90, "assistant", "action_item", "Open the walkthrough site on Phone?", {"item_id": "card01", "status": "answered", "kind": "choice", "question": "Open the walkthrough site on Phone?"}),
    ]
    for t, role, kind, text, meta in msgs:
        conn.execute("insert into messages(bot_id,role,kind,content,meta,created,updated) values(?,?,?,?,?,?,?)", (bot, role, kind, text, json.dumps(meta), base + t, base + t))
    conn.execute("insert into items values(?,?,?,?,?,?,?,?,?,?)", ("card01", bot, None, 6, "choice", json.dumps({"kind": "choice", "question": "Open the walkthrough site on Phone?", "options": ["Open it", "Show me the link", "None, I'll decide later"]}),
                                                                       "answered", "Show me the link", base + 90, base + 98))
    plans = [
        ("job-emu", base + 3, base + 32, "failed", "", "Open the sample game on Phone and report what it shows",
         [("Opening the app list to find the game.", {"type": "find_app", "query": "sample game"}, ("Home", ["Sample game", "Settings", "Clock"]), False),
          ("Found it, opening the app.", {"type": "open_app", "package": "example.samplegame"}, ("Sample game", ["Loading…"]), False),
          ("The app shows a crash dialog; closing it and trying once more.", {"type": "tap", "x": 300, "y": 740}, ("Sample game", ["Loading…"]), True),
          ("It crashed 3 times, so I'm stopping.", {"type": "fail", "reason": "App keeps crashing: example.samplegame crashed 3 times while opening"}, ("Home", ["Sample game"]), False)]),
        ("job-games", base + 36, base + 60, "done", "virtual:games", "Open the sample game on Phone 2 and report what it shows",
         [("Opening the game on Phone 2.", {"type": "open_app", "package": "example.samplegame"}, ("Home", ["Sample game"]), False),
          ("It stayed up and shows its start screen with a Play button.", {"type": "done", "answer": "The start screen with a Play button is showing; nothing was tapped."}, ("Sample game", ["Play", "Settings"]), False)]),
    ]
    for jid, start, end, status, target, goal, steps in plans:
        trace = runs / jid
        trace.mkdir(parents=True, exist_ok=True)
        timing = []
        for n, (thought, action, (title, rows), crash) in enumerate(steps, 1):
            (trace / f"step{n:02d}.png").write_bytes(_screen(title, rows, crash=crash))
            (trace / f"step{n:02d}.json").write_text(json.dumps({"prompt": "Goal: demo\n\nUI elements:\n0: 'Sample game' at (100,200) [clickable]\n", "reply": {"thought": thought, "action": action}}))
            timing.append(json.dumps({"step": n, "observe": 300, "model": 2800, "act": 400, "settle": 700}))
        (trace / "timing.jsonl").write_text("\n".join(timing) + "\n")
        result = steps[-1][1].get("answer") or steps[-1][1].get("reason", "")
        conn.execute("insert into jobs(id,bot_id,goal,status,started,ended,result,trace_dir,after,verify,routine,target,files) values(?,?,?,?,?,?,?,?,?,?,?,?,?)", (jid, bot, goal, status, start, end, result, str(trace), None, None, None, target or None, None))
    conn.commit()
    conn.close()
    return bot


def export_demo(out: Path, name: str | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bot = make_demo(tmp / "demo.db", tmp / "runs")
        conn = sqlite3.connect(tmp / "demo.db")
        manifest, files = build_session(conn, bot, tmp, title="Synthetic demo: an app that crashes on Phone and runs on Phone 2",
                                        name=name or out.name, synthetic=True)
        conn.close()
        write_session(out, manifest, files)
        return manifest


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def check_folder(root: Path, extra: tuple[str, ...] = ()) -> list[str]:
    """Problems found in published sessions: any text that `mask_text` would still change, a picture that is not a small JPEG, an unsafe file name.
    Run this before committing web/replay/ (the Pages workflow runs it too)."""
    problems = []
    for sj in sorted(root.glob("*/session.json")):
        try:
            data = json.loads(sj.read_text())
        except ValueError as e:
            problems.append(f"{sj}: not valid JSON ({e})")
            continue
        for text in _strings(data):
            if mask_text(text, extra) != text:
                problems.append(f"{sj}: unmasked text {text[:60]!r}")
        for img in sj.parent.glob("img/*"):
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.jpg", img.name):
                problems.append(f"{img}: unexpected file name")
                continue
            try:
                with Image.open(img) as im:
                    if im.format != "JPEG" or im.width > SHOT_WIDTH:
                        problems.append(f"{img}: not a JPEG of at most {SHOT_WIDTH} px")
            except OSError:
                problems.append(f"{img}: not a picture")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bot", nargs="?", help="bot id to export (omit with --demo)")
    ap.add_argument("--demo", action="store_true", help="export a deterministic synthetic session instead of real data")
    ap.add_argument("--check", metavar="DIR", help="check every session under DIR (for example web/replay) for text that is not masked and for bad pictures; exit 1 on problems")
    ap.add_argument("--out", help="output folder, for example web/replay/hero-a (the folder name is the session name)")
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"), help="database file (copied, never written)")
    ap.add_argument("--title")
    ap.add_argument("--from-msg", type=int, default=0)
    ap.add_argument("--to-msg", type=int, default=10**12)
    ap.add_argument("--max-gap", type=float, default=MAX_GAP_S, help="longest idle gap kept, in seconds")
    ap.add_argument("--mask", action="append", default=[], help="extra text to hide everywhere (the owner's name, account, a city); repeatable")
    ap.add_argument("--public-images", action="store_true", help="keep pictures from the owner's chat as thumbnails (default: a grey placeholder)")
    ap.add_argument("--redact", action="append", default=[], help="x0,y0,x1,y1 as fractions of the screenshot to black out; repeatable")
    args = ap.parse_args(argv)
    if args.check:
        problems = check_folder(Path(args.check), tuple(args.mask))
        for line in problems:
            print(line)
        print(f"{args.check}: {'OK' if not problems else str(len(problems)) + ' problem(s)'}")
        return 1 if problems else 0
    if not args.out:
        ap.error("--out is required")
    out = Path(args.out)
    if args.demo:
        m = export_demo(out)
    else:
        if not args.bot:
            ap.error("give a bot id or --demo")
        redact = [tuple(float(x) for x in r.split(",")) for r in args.redact]
        if any(len(r) != 4 for r in redact):
            ap.error("--redact needs four numbers")
        dbfile = Path(args.db)
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "copy.db"
            src = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
            dst = sqlite3.connect(copy)
            src.backup(dst)
            src.close()
            m, files = build_session(dst, args.bot, dbfile.parent, title=args.title or "", from_msg=args.from_msg, to_msg=args.to_msg, max_gap=args.max_gap,
                                     mask=tuple(args.mask), public_images=args.public_images, redact=redact, name=out.name)
            dst.close()
        write_session(out, m, files)
    print(f"{out}: {m['title']!r}, {len(m['events'])} events, {m['steps']} steps, {m['duration_ms'] / 1000:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
