"""Reset the app to a first-run state: wipe chats, tasks, memory, routines, screenshots and saved traces, then re-seed the bots.

Stop the service first (it holds the database open), then: .venv/bin/python scripts/reset_fresh.py [--keep-phones]
Kept: saved secrets (vault), paired devices (data/state.json) and the emulator itself. The phones' own bots (Phone 2, real phones) are
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
    keep = {"master", "virtual", "phone"}
    for b in db.list_bots():
        if b["kind"] not in keep:
            db.delete_bot(b["id"])
    with db.lock:
        for table in ("messages", "jobs", "items", "library", "memory", "routines", "alerts", "vault_audit", "attachments"):
            db.conn.execute(f"delete from {table}")
        db.conn.commit()
    for b in db.list_bots():
        text = {"master": INTRO, "virtual": GAMES_INTRO}.get(b["kind"]) or PHONE_GREETING.format(name=b["name"])
        db.add_message(b["id"], "assistant", "text", text)
    if not [b for b in db.list_bots() if b["kind"] == "bot"]:
        db.create_bot("Assistant", DEFAULT_ASSISTANT_PERSONA)
    for d in ("library", "runs", "uploads"):  # uploads: the pictures shared in chat
        p = ROOT / "data" / d
        if p.exists():
            shutil.rmtree(p)
            p.mkdir()
    print("Fresh start. Bots:", ", ".join(f"{b['name']} ({b['kind']})" for b in db.list_bots()), "| backup:", backup.name)


if __name__ == "__main__":
    main()
