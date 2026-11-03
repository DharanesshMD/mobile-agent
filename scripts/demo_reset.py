"""Put the app in the demo state: a clean first-run database, the demo bots, one routine, one vault entry, two memories, and the phones ready to film.

Stop the service first (it holds the database open); this script refuses to run while one is up or a job is running. Then:
    .venv/bin/python scripts/demo_reset.py [--no-phones] [--fresh-chain]

--fresh-chain is for filming Hero A (an app that crashes on Phone is moved to Phone 2): it forgets where apps already ran and removes Clash of Clans from the
Phone 2 (starting that phone if needed), so the whole chain shows. Without it a second take goes straight to Phone 2, which is also a good thing to film.
and start the service again (preview_start "mobile-agent", or `uv run python -m agent.service`). Run scripts/demo_check.py to see what is green.

What it does, in order:
 1. backs up the database (data/app.db.before-demo-reset-<time>);
 2. wipes chats, tasks, memory, routines, screenshots, pictures and traces (like scripts/reset_fresh.py), keeps the vault's other secrets, paired devices and phone bots;
 3. seeds: Master with its introduction, "Home assistant" (persona set), the Phone 2 bot, the real-phone bot renamed "My phone" when there is one, a "Morning brief" routine
    (daily 07:30 local time, web search), a vault entry "Demo login" with a clearly fake value, and two memories (favourite colour, Mom's demo number);
 4. prepares the phones (skipped with --no-phones): emulator awake on the launcher with the demo screen profile and the Mac's time zone, three FAKE contacts, Chrome and
    Calendar first-run dialogs dismissed; Phone 2 checked for Shattered Pixel Dungeon and Clash of Clans (opened, never played).

PRIVACY: the emulator's Google account syncs that account's own contacts and calendar. This script never reads or changes them, but they are on the phone: do not film
the Contacts or Calendar lists unless that account is a throwaway.
"""
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from agent.db import Database  # noqa: E402
from agent.master import DEFAULT_ASSISTANT_PERSONA, GAMES_INTRO, INTRO, ensure_games_bot  # noqa: E402
from agent.routines import next_run  # noqa: E402
from agent.vault import Vault, load_key  # noqa: E402

DB = ROOT / "data" / "app.db"
EMULATOR = os.environ.get("ANDROID_SERIAL", "emulator-5554")
GAMES = "localhost:5565"
HOME_PERSONA = "The owner's home assistant: answers questions, remembers what the owner tells it, and uses the phone for errands."
PHONE_GREETING = "{name} is paired. Tell me what to do on it: I work on it in the background, and these tasks run only on this phone."
BRIEF = ("Give me a short morning brief: today's date, the weather in my city if you can find it, and the three biggest news headlines of the day. "
         "Use web search, keep it to five lines, and say where each fact came from.")
DEMO_SECRET = ("Demo login", "demo-not-a-real-password", "password")
MEMORIES = ["The owner's favourite colour is teal.", "In the demo the owner's mother is saved as 'Mom (demo)' with the fake number 555-0100."]
FAKE_CONTACTS = [("Mom (demo)", "5550100"), ("Alex Demo", "5550101"), ("Sam Demo", "5550102")]
FIRST_RUN_LABELS = ["use without an account", "no thanks", "no, thanks", "not now", "skip", "got it", "accept & continue", "accept and continue", "while using the app",
                    "allow", "don't allow", "next", "done", "ok"]
APPS = {"chrome": "com.android.chrome", "calendar": "com.google.android.calendar"}
GAME_APPS = {"Shattered Pixel Dungeon": "com.shatteredpixel.shatteredpixeldungeon", "Clash of Clans": "com.supercell.clashofclans"}


def adb_path() -> str:
    found = shutil.which("adb")
    sdk = Path(os.environ.get("ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools")) / "platform-tools" / "adb"
    return found or (str(sdk) if sdk.exists() else "adb")


def adb(serial: str, *args: str, timeout: float = 20) -> str:
    """adb joins the arguments after `shell` into one string for the phone's shell, so quote each one (a contact called "Mom (demo)" must stay one value)."""
    if args and args[0] == "shell":
        args = ("shell", " ".join(shlex.quote(a) for a in args[1:]))
    return subprocess.run([adb_path(), "-s", serial, *args], capture_output=True, text=True, timeout=timeout).stdout


def service_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-f", "--", "-m agent.service"], capture_output=True, text=True).stdout.split()
    me = os.getpid()
    return [int(p) for p in out if int(p) != me]


def refuse_if_busy(db_path: Path) -> None:
    pids = service_pids()
    if pids:
        sys.exit(f"The service is running (pid {', '.join(map(str, pids))}). Stop it first: it holds the database open.")
    if db_path.exists():
        n = sqlite3.connect(db_path).execute("select count(*) from jobs where status in ('queued','running','waiting')").fetchone()[0]
        if n:
            sys.exit(f"{n} job(s) are still queued or running in the database; let them finish or cancel them first.")


def wipe_and_seed(db: Database) -> dict:
    keep = {"master", "virtual", "phone"}
    for b in db.list_bots():
        if b["kind"] not in keep:
            db.delete_bot(b["id"])
    with db.lock:
        for table in ("messages", "jobs", "items", "library", "memory", "routines", "alerts", "vault_audit", "attachments", "launch_facts"):
            db.conn.execute(f"delete from {table}")
        db.conn.commit()
    for d in ("library", "runs", "uploads"):
        p = ROOT / "data" / d
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    master = db.master_bot() or db.create_bot("Master", "", kind="master")
    db.add_message(master["id"], "assistant", "text", INTRO)
    home = db.create_bot("Home assistant", HOME_PERSONA)
    games = ensure_games_bot(db, None) if (shutil.which("limactl") and (Path.home() / ".lima" / "rd").exists()) else db.phone_bot("games", "virtual")
    if games:
        db.update_message(db.messages(games["id"], 1)[0]["id"], content=GAMES_INTRO) if db.messages(games["id"], 1) else db.add_message(games["id"], "assistant", "text", GAMES_INTRO)
    for b in db.list_bots():
        if b["kind"] == "phone":
            db._x("update bots set name=? where id=?", ("My phone", b["id"]))
            db.add_message(b["id"], "assistant", "text", PHONE_GREETING.format(name="Phone 3"))
    sched = {"daily_at": "07:30"}
    db.routine_add(home["id"], "Morning brief", BRIEF, sched, next_run(sched, time.time()))
    for text in MEMORIES:
        db.memory_add(home["id"], text)
    vault = Vault(db, load_key())
    if DEMO_SECRET[0] not in vault.labels():
        vault.put(*DEMO_SECRET)
    return {"home": home["id"], "master": master["id"]}


def dismiss_first_run(serial: str, package: str, rounds: int = 8) -> list[str]:
    """Open an app and tap through its welcome and permission dialogs (preferring No thanks / Skip), then go Home. Returns the labels tapped."""
    from agent.device import Device
    dev = Device(serial)
    tapped: list[str] = []
    adb(serial, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(3)
    for _ in range(rounds):
        try:
            els = dev.elements()
        except Exception:
            break
        pick = None
        for want in FIRST_RUN_LABELS:  # the order above is the preference
            pick = next((e for e in els if e.clickable and e.label.strip().lower() == want), None)
            if pick:
                break
        if not pick:
            break
        dev.tap(*pick.center)
        tapped.append(pick.label)
        time.sleep(1.5)
    adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
    return tapped


def add_fake_contacts(serial: str) -> int:
    """Device-only contacts (no account), so they are never synced anywhere. Skips names that already exist."""
    have = adb(serial, "shell", "content", "query", "--uri", "content://com.android.contacts/data", "--projection", "data1",
               "--where", "mimetype='vnd.android.cursor.item/name'")
    added = 0
    for name, number in FAKE_CONTACTS:
        if f"data1={name}" in have:
            continue
        adb(serial, "shell", "content", "insert", "--uri", "content://com.android.contacts/raw_contacts", "--bind", "account_type:n:", "--bind", "account_name:n:")
        rows = adb(serial, "shell", "content", "query", "--uri", "content://com.android.contacts/raw_contacts", "--projection", "_id", "--where", "account_type IS NULL")
        ids = [int(part.split("=")[1]) for line in rows.splitlines() for part in line.replace(",", " ").split() if part.startswith("_id=")]
        if not ids:
            continue
        rid = str(max(ids))
        adb(serial, "shell", "content", "insert", "--uri", "content://com.android.contacts/data", "--bind", f"raw_contact_id:i:{rid}", "--bind",
            "mimetype:s:vnd.android.cursor.item/name", "--bind", f"data1:s:{name}")
        adb(serial, "shell", "content", "insert", "--uri", "content://com.android.contacts/data", "--bind", f"raw_contact_id:i:{rid}", "--bind",
            "mimetype:s:vnd.android.cursor.item/phone_v2", "--bind", f"data1:s:{number}", "--bind", "data2:i:2")
        added += 1
    return added


def fresh_chain() -> list[str]:
    """Take Clash of Clans off Phone 2 so Hero A shows the whole chain (crash on Phone, copy, open on Phone 2)."""
    notes: list[str] = []
    script = ROOT / "scripts" / "virtual-android" / "redroid.sh"
    if adb(GAMES, "get-state", timeout=6).strip() != "device":
        r = subprocess.run(["bash", str(script), "start"], capture_output=True, text=True, timeout=240)
        notes.append("Phone 2 started for the clean-up" if r.returncode == 0 else f"Phone 2 did not start: {(r.stdout + r.stderr)[-120:]}")
    out = adb(GAMES, "uninstall", "com.supercell.clashofclans", timeout=60).strip()
    notes.append(f"Clash of Clans removed from Phone 2: {out or 'no answer'}")
    return notes


def prepare_phones() -> list[str]:
    from agent.device import Device
    from agent.lifecycle import EmulatorBackend
    notes: list[str] = []
    backend = EmulatorBackend([adb_path(), "-s", EMULATOR])
    state = backend.status()
    if state == "stopped":
        notes.append("Phone is stopped: start the service, wake the phone from the Mobile tab, then run this script's phone step again (--phones-only)")
        return notes
    if state == "paused" and not backend.resume():
        notes.append("Phone did not resume")
        return notes
    try:
        dev = Device(EMULATOR)
        dev.prepare()
        notes.append(f"Phone: demo screen profile and time zone {dev.sync_timezone() or '(unknown)'}")
        notes.append(f"fake contacts added: {add_fake_contacts(EMULATOR)}")
        for name, pkg in APPS.items():
            if f"package:{pkg}" in adb(EMULATOR, "shell", "pm", "list", "packages", pkg):
                notes.append(f"{name}: dismissed {dismiss_first_run(EMULATOR, pkg) or 'nothing (already ready)'}")
            else:
                notes.append(f"{name}: NOT installed")
        adb(EMULATOR, "shell", "input", "keyevent", "KEYCODE_HOME")
    except Exception as e:  # a phone that does not answer must not hide the database result
        notes.append(f"Phone step failed: {type(e).__name__}: {str(e)[:120]}")
    try:
        if adb(GAMES, "get-state", timeout=6).strip() != "device":
            notes.append("Phone 2: off (it starts by itself when a task needs it): app check skipped, demo_check.py checks it once it is up")
            return notes
        have = adb(GAMES, "shell", "pm", "list", "packages", timeout=10)
        for label, pkg in GAME_APPS.items():
            notes.append(f"Phone 2 {label}: {'installed' if f'package:{pkg}' in have else 'NOT installed (scripts/virtual-android/install-apk.sh)'}")
    except Exception:
        notes.append("Phone 2: not answering; app check skipped")
    return notes


def main() -> None:
    args = sys.argv[1:]
    if "--phones-only" in args:
        print("\n".join(prepare_phones()))
        return
    refuse_if_busy(DB)
    if DB.exists():
        backup = DB.with_name(f"app.db.before-demo-reset-{time.strftime('%Y%m%d-%H%M%S')}")
        src, dst = sqlite3.connect(DB), sqlite3.connect(backup)
        src.backup(dst)
        src.close()
        dst.close()
        print("backup:", backup.name)
    db = Database(str(DB))
    ids = wipe_and_seed(db)
    print("bots:", ", ".join(f"{b['name']} ({b['kind']})" for b in db.list_bots()))
    print("seeded: Morning brief 07:30, vault 'Demo login', 2 memories")
    if "--no-phones" not in args:
        print("\n".join(prepare_phones()))
    if "--fresh-chain" in args:
        print("\n".join(fresh_chain()))
    print("\nStart the service, then run scripts/demo_check.py")
    print(json.dumps(ids))


if __name__ == "__main__":
    main()
