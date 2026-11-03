"""Reset the app to a first-run state: wipe chats, tasks, memory and shared knowledge, routines, A2A task history, screenshots, saved traces and log files, then re-seed the bots.

Stop the service first (it holds the database open), then: .venv/bin/python scripts/reset_fresh.py [--keep-phones]
Kept: saved secrets (vault), connected apps (connectors), A2A callers and their tokens, paired devices (data/state.json), what the app learned about which apps run on which phone, and the emulator itself. The phones' own bots (Phone 2, real phones) are
kept with a single greeting each, so a connected phone still has its bot. A backup of the database is written next to it first.
"""
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.db import Database  # noqa: E402
from agent.master import DEFAULT_ASSISTANT_PERSONA, GAMES_INTRO, INTRO  # noqa: E402

DB = ROOT / "data" / "app.db"
PHONE_GREETING = "{name} is paired. Tell me what to do on it: I work on it in the background, and these tasks run only on this phone."


def main() -> None:
    backup = DB.with_name(f"app.db.before-reset-{time.strftime('%Y%m%d-%H%M%S')}")
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    src.close()
    dst.close()
    db = Database(str(DB))
    keep = {"master", "virtual", "phone", "agent"}  # an A2A caller's bot stays, so the caller keeps working (with an empty chat)
    for b in db.list_bots():
        if b["kind"] not in keep:
            db.delete_bot(b["id"])
    with db.lock:
        for table in ("messages", "jobs", "items", "library", "memory", "routines", "alerts", "vault_audit", "attachments",
                      "a2a_tasks", "a2a_messages", "a2a_artifacts", "a2a_files", "a2a_events", "a2a_push", "a2a_audit"):
            try:
                db.conn.execute(f"delete from {table}")
            except sqlite3.OperationalError:  # a table of a feature this database never used
                pass
        db.conn.commit()
    db.knowledge.erase_all()  # the shared knowledge store (and its search index)
    for b in db.list_bots():
        if b["kind"] == "agent":
            continue
        text = {"master": INTRO, "virtual": GAMES_INTRO}.get(b["kind"]) or PHONE_GREETING.format(name=b["name"])
        db.add_message(b["id"], "assistant", "text", text)
    if not db.master_bot() and not [b for b in db.list_bots() if b["kind"] == "bot"]:  # with the master bot the list starts as just that
        db.create_bot("Assistant", DEFAULT_ASSISTANT_PERSONA)
    for d in ("library", "runs", "uploads"):  # uploads: the pictures shared in chat
        p = ROOT / "data" / d
        if p.exists():
            shutil.rmtree(p)
            p.mkdir()
    logs = ROOT / "data" / "logs"
    for f in list(logs.glob("*.log*")) if logs.exists() else []:  # the log files start empty too
        f.unlink(missing_ok=True)
    print("Fresh start. Bots:", ", ".join(f"{b['name']} ({b['kind']})" for b in db.list_bots()), "| backup:", backup.name)


if __name__ == "__main__":
    main()
