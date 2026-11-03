"""The master bot: sees every other bot, reports what is missing or stuck, and raises alerts without calling a model.

`digest` is the text the master's model reads each turn. `find_alerts` and `Watcher` are deterministic: each problem is posted once in the
master's chat (and shown as a badge and browser notification by the web page), so the owner is told even when nobody asks.
"""
import re
import threading
import time

from agent.db import Database
from agent.hub import Hub

WAITING_MIN = 10          # an unanswered card older than this is reported
RECENT_FAILURE_H = 24     # failed tasks newer than this are reported
CHECK_SECONDS = 60
DIGEST_CHARS = 4500
INTRO = ("I'm your master bot. I can see all your other bots, so ask me what any of them is doing, what is missing, or who is waiting on you. "
         "I'll also post here when a bot is stuck, a question has been waiting too long, or a task failed.")


def ago(seconds: float) -> str:
    m = int(seconds // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 90 else f"{m // 60} h ago" if m < 2880 else f"{m // 1440} days ago"


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def ensure_master(db: Database, hub: Hub | None = None) -> dict:
    """The one master bot, created with a short introduction the first time."""
    m = db.master_bot()
    if m:
        return m
    m = db.create_bot("Master", "", kind="master")
    msg = db.add_message(m["id"], "assistant", "text", INTRO)
    if hub:
        hub.publish(m["id"], {"type": "message", "message": msg})
    return m


DEFAULT_ASSISTANT_PERSONA = "A general assistant. Ask it anything; it uses the shared phone when a task needs one."
RAW_MODEL_NAME = re.compile(r"sdk_|gphone|generic|emulator|redroid|^[a-z0-9]+(_[a-z0-9]+)+$", re.I)


def phone_name(info: dict) -> str:
    """A friendly bot name for a phone: the device name unless it is a raw model string (`sdk_gphone64_arm64`), then "My phone"."""
    name = str(info.get("name") or info.get("model") or "").strip()[:40]
    return "My phone" if not name or RAW_MODEL_NAME.search(name) else name


PHONE_PERSONA = ("The bot of your own phone. It works on that phone in the background through the Mobile Agent app: every task you give it runs there, "
                 "and nothing else uses that phone.")


def ensure_phone_bot(db: Database, hub: Hub | None, phone_id: str, info: dict) -> dict:
    """The persistent bot of a real phone, created the first time the phone connects and kept (with its chat and routines) across reconnects."""
    bot = db.phone_bot(phone_id)
    if bot:
        return bot
    name = phone_name(info)
    taken = {b["name"] for b in db.list_bots()}
    if name in taken:  # a second phone (or the same phone paired again) must not get a bot with the same name
        n = 2
        while f"{name} {n}" in taken:
            n += 1
        name = f"{name} {n}"
    bot = db.create_bot(name, PHONE_PERSONA, kind="phone", phone_id=phone_id)
    msg = db.add_message(bot["id"], "assistant", "text", f"Your phone ({name}) is paired. Tell me what to do on it: I work on it in the background, and these tasks never touch the shared emulator.")
    if hub:
        hub.publish(bot["id"], {"type": "message", "message": msg})
    return bot


GAMES_NAME = "Cloud phone 2"
GAMES_PERSONA = ("The bot of Cloud phone 2: a second light Android (a container) for heavy apps and anything the shared Cloud phone cannot run. It runs only here, "
                 "in the background, and never uses the shared Cloud phone.")
GAMES_INTRO = ("This is Cloud phone 2: a second light Android for heavy apps and anything the shared Cloud phone cannot run. What to expect, honestly: there is no Google Play here, "
               "so apps are installed from APK files; graphics are software-rendered (low quality); apps that draw one picture, like games, have no readable screen, so I work from "
               "screenshots and may need a few tries; and some apps refuse to run on virtual devices. For those, connect your own phone with the Mobile Agent app.")


def ensure_games_bot(db: Database, hub: Hub | None, phone_id: str = "games") -> dict:
    """The persistent bot of Cloud phone 2, created at start-up when the phone exists on this computer."""
    bot = db.phone_bot(phone_id, "virtual")
    if bot:
        if bot["name"] == "Games phone":  # the old display name: keep the bot and its history, give it the generic name
            bot = db.rename_bot(bot["id"], GAMES_NAME, GAMES_PERSONA) or bot
        return bot
    bot = db.create_bot(GAMES_NAME, GAMES_PERSONA, kind="virtual", phone_id=phone_id)
    msg = db.add_message(bot["id"], "assistant", "text", GAMES_INTRO)
    if hub:
        hub.publish(bot["id"], {"type": "message", "message": msg})
    return bot


def ensure_adb_bot(db: Database, hub: Hub | None, name: str, serial: str, has_play: bool) -> dict:
    """The persistent bot of an extra adb phone (ADB_PHONES), created once adb reaches it. Its tasks run only on that phone."""
    bot = db.phone_bot(name, "virtual")
    if bot:
        return bot
    play = "Google Play is installed here, so apps can be installed from the Play Store." if has_play else "There is no Google Play here, so apps come as APK files."
    bot = db.create_bot(f"{name.title()} phone", f"The bot of the extra adb phone '{name}' ({serial}). It runs only on that phone, in the background, and never uses the shared emulator.",
                        kind="virtual", phone_id=name)
    msg = db.add_message(bot["id"], "assistant", "text", f"This is the extra phone '{name}', reached over adb ({serial}). {play} It is always on while adb can reach it; "
                         "if it goes offline, my tasks fail and I say so instead of using another phone.")
    if hub:
        hub.publish(bot["id"], {"type": "message", "message": msg})
    return bot


def _describe(r: dict) -> str:
    s = r["schedule"]
    return f"{r['name']} ({'daily at ' + s['daily_at'] if 'daily_at' in s else 'every ' + str(s['every_minutes']) + ' min'}{'' if r['enabled'] else ', paused'})"


def digest(db: Database, now: float | None = None) -> str:
    now = now or time.time()
    out: list[str] = []
    for b in db.list_bots():
        if b["kind"] == "master":
            continue
        bid = b["id"]
        lines = [f"- {b['name']} (focus: {_clip(b['persona'], 160) or 'NOT SET'})"]
        msgs = [m for m in db.messages(bid, 12) if m["kind"] in ("text", "result")][-3:]
        for m in msgs:
            lines.append(f"    {'owner' if m['role'] == 'user' else 'bot'} {ago(now - m['created'])}: {_clip(m['content'], 110)}")
        if not msgs:
            lines.append("    no conversation yet")
        jobs = db.jobs(bid, 5)
        active = [j for j in jobs if j["status"] in ("queued", "running", "waiting")]
        if active:
            lines.append(f"    phone task {active[0]['status']}: {_clip(active[0]['goal'], 100)}")
        failed = [j for j in jobs if j["status"] in ("failed", "max_steps") and now - (j["ended"] or 0) < RECENT_FAILURE_H * 3600]
        for j in failed[:2]:
            lines.append(f"    FAILED task {ago(now - j['ended'])}: {_clip(j['goal'], 80)} -> {_clip(j['result'], 100)}")
        for it in db.open_items(bid):
            lines.append(f"    WAITING for the owner {ago(now - it['created'])}: {_clip(it['payload'].get('question', ''), 110)}")
        routines = db.routines(bid)
        if routines:
            lines.append("    routines: " + "; ".join(_describe(r) for r in routines))
        facts = db.memory_recent(bid, 3)
        if facts:
            lines.append("    remembers: " + " | ".join(_clip(f, 70) for f in facts))
        out.append("\n".join(lines))
    text = "\n".join(out) or "(no other bots yet)"
    return text if len(text) <= DIGEST_CHARS else text[:DIGEST_CHARS] + "\n…(digest truncated)"


def find_alerts(db: Database, now: float | None = None) -> list[dict]:
    """Everything the owner should hear about now. Each alert has a stable key so it is only ever posted once."""
    now = now or time.time()
    found: list[dict] = []
    for b in db.list_bots():
        if b["kind"] == "master":
            continue
        bid, name = b["id"], b["name"]
        if not b["persona"].strip():
            found.append({"key": f"focus:{bid}", "bot": b, "text": f"{name} has no focus yet. Tell it what it is for (rename it from the ⋯ menu) so it can help properly."})
        for it in db.open_items(bid):
            if now - it["created"] >= WAITING_MIN * 60:
                found.append({"key": f"item:{it['id']}", "bot": b, "text": f"{name} has been waiting for you for {ago(now - it['created']).replace(' ago', '')}: {_clip(it['payload'].get('question', ''), 120)}"})
        for j in db.jobs(bid, 10):
            if j["status"] in ("failed", "max_steps") and j["ended"] and now - j["ended"] < RECENT_FAILURE_H * 3600:
                found.append({"key": f"job:{j['id']}", "bot": b, "text": f"{name}: a phone task failed ({_clip(j['goal'], 80)}). {_clip(j['result'], 120)}"})
    return found


ALERT_TTL_H = 24


def resolved(db: Database, alert: dict, now: float) -> bool:
    """Is the problem an old alert message was about gone? Alerts older than ALERT_TTL_H are always cleared."""
    if now - alert["created"] > ALERT_TTL_H * 3600:
        return True
    meta, key = alert["meta"], alert["meta"].get("key", "")
    bot = db.get_bot(meta.get("bot_id", "")) if meta.get("bot_id") else None
    if meta.get("bot_id") and not bot:
        return True
    if key.startswith("focus:"):
        return bool(bot and bot["persona"].strip())
    if key.startswith("item:"):
        return key[5:] not in [i["id"] for i in db.open_items(bot["id"])]
    if key.startswith("job:"):
        job = db.get_job(key[4:])
        return not job or any(j["status"] == "done" and (j["ended"] or 0) > (job["ended"] or 0) for j in db.jobs(bot["id"], 20))
    return False


class Watcher:
    def __init__(self, db: Database, hub: Hub, clock=time.time):
        self.db, self.hub, self.clock = db, hub, clock
        self._stop = threading.Event()

    def check(self) -> int:
        master = self.db.master_bot()
        if not master:
            return 0
        n = 0
        for a in find_alerts(self.db, self.clock()):
            if self.db.alert_once(a["key"]):
                msg = self.db.add_message(master["id"], "assistant", "alert", a["text"], {"bot_id": a["bot"]["id"], "bot": a["bot"]["name"], "key": a["key"]})
                self.hub.publish(master["id"], {"type": "message", "message": msg})
                n += 1
        self.clear_stale(master["id"])
        return n

    def clear_stale(self, master_id: str) -> int:
        """Remove alerts whose situation is gone (the bot got a focus or was deleted, the card was answered, a later task of that bot succeeded) and any alert older than 24 h."""
        now, gone = self.clock(), 0
        for m in self.db.messages(master_id, 200):
            if m["kind"] != "alert":
                continue
            if resolved(self.db, m, now):
                self.db.delete_message(m["id"])
                self.hub.publish(master_id, {"type": "message_delete", "id": m["id"]})
                gone += 1
        return gone

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(CHECK_SECONDS):
                try:
                    self.check()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True, name="master-watch").start()

    def stop(self) -> None:
        self._stop.set()
