"""A directory of every phone the agent can use, what each can and cannot do, how it is doing right now, which Google account each has, and which apps are known not to run on it.

Every bot gets a short version in its context, so a task goes to a phone that can really do it (route by capability) and no bot offers an action its phone
cannot do (for example a Play Store install on Cloud phone 2). Master gets how to route as well.
"""
import re
import threading
import time

from agent.graph import CRASH_PREFIX

CRASH_RE = re.compile(re.escape(CRASH_PREFIX) + r"([\w.]+) crashed")

EMULATOR = ("Cloud phone (the shared emulator): Google Play and Google sign-in work here, and Google apps, system apps and intents (alarm, SMS, maps). It is the only phone to "
            "install from the Play Store on. Some apps and games detect it and refuse to run. It can receive an app moved from Cloud phone 2 (move_app).")
GAMES = ("Cloud phone 2: a second light Android for heavy apps and anything the shared Cloud phone cannot run. Apps reach it only as APK files that the owner installs, or moved from the emulator with "
         "move_app (the app is copied, never its data or sign-in): NO Google Play, NO Google apps or Google sign-in, so it can never have a Google account. Games draw one "
         "picture (no UI tree), software graphics, and some games still refuse to run on a virtual device.")
ADB = ("An extra adb phone the owner manages ({serial}; a real phone over USB debugging, BlueStacks with adb, or a cloud phone). It is always on while adb reaches it and has no power "
       "management, so if it goes offline a task fails honestly. {play} It can receive an app moved from the emulator or Cloud phone 2 (move_app).")
REAL = ("The owner's own phone: genuine hardware with their own apps and accounts, driven in the background through the Mobile Agent app. It works only while the app is "
        "connected. It installs from its own Play Store: apps cannot be moved to or from it, so route a normal task to its bot.")

POWER_TEXT = {"on": "ready", "sleeping": "asleep (wakes by itself in a moment)", "off": "off (starts by itself)", "waking": "waking up", "unavailable": "not set up"}
ACCOUNT_TTL = 120  # seconds an account fact is reused: reading it costs an adb call and the directory is built for every turn
ACCOUNT_WAIT = 3  # a phone that does not answer in this many seconds is reported as unknown instead of holding up the chat


def crash_facts(db) -> list[dict]:
    """Apps that crashed on their own on a phone during a task, from the jobs table: [{"phone": "emulator|games|phone:<id>", "package", "goal"}], newest first, one per (phone, package)."""
    rows = db._q("select target, goal, result from jobs where status='failed' and result like ? order by ended desc limit 100", (CRASH_PREFIX + "%",))
    seen, out = set(), []
    for f in db.launch_facts():  # a launch the agent checked itself (agent/escalate.py): it crashed or would not stay up
        if not f["ok"] and (f["phone"], f["package"]) not in seen:
            seen.add((f["phone"], f["package"]))
            out.append({"phone": f["phone"], "package": f["package"], "goal": (f["note"] or "")[:80]})
    for r in rows:
        m = CRASH_RE.match(r["result"])
        if not m:
            continue
        t = r["target"] or ""
        phone = "emulator" if not t else t.split(":", 1)[1] if t.startswith("virtual:") else t
        if (phone, m.group(1)) not in seen:
            seen.add((phone, m.group(1)))
            out.append({"phone": phone, "package": m.group(1), "goal": r["goal"][:80]})
    return out


def phone_key(bot: dict) -> str:
    """Which phone a bot's tasks run on: emulator, games, or phone:<id>."""
    if bot.get("kind") == "virtual" and bot.get("phone_id"):
        return bot["phone_id"]
    if bot.get("kind") == "phone" and bot.get("phone_id"):
        return f"phone:{bot['phone_id']}"
    return "emulator"


def _bounded(fn, secs: float):
    """fn() with a time limit: (True, value) or (False, None) if it raised or did not answer in time."""
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    return ("v" in box), box.get("v")


def google_account_fact(jobs, key: str, dev, lc) -> str:
    """Is a Google account signed in on this phone? Never names the account. Cached briefly, and a phone that is asleep or silent is 'unknown', not asked."""
    if key == "games":
        return "none (it has no Google services, so none can be signed in)"
    if dev is None or not hasattr(dev, "accounts"):
        return "unknown (ask with phone_check kind account)"
    if lc is not None and (lc.info() or {}).get("power") != "on":
        return "unknown (the phone is asleep)"
    cache = jobs.__dict__.setdefault("_account_cache", {})
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ACCOUNT_TTL:
        return hit[1]
    ok, accounts = _bounded(dev.accounts, ACCOUNT_WAIT)
    if not ok:
        return "unknown (the phone did not answer)"
    n = len([a for a in accounts if a[1] == "com.google"])
    text = f"{n} Google account(s) signed in" if n else "no Google account signed in"
    cache[key] = (time.time(), text)
    return text


APPS_TTL = 120  # seconds an installed-apps fact is reused
APPS_SHOWN = 14


def installed_apps_fact(jobs, key: str, dev, lc) -> str:
    """The apps installed on a phone (third-party ones), so a task goes to the phone that already has its app. A phone that is asleep is not asked: the list from the
    last time it was awake is shown, marked as such, so choosing a phone never wakes one."""
    cache = jobs.app_cache
    awake = lc is None or (lc.info() or {}).get("power") == "on"
    hit = cache.get(key)
    if awake and dev is not None and hasattr(dev, "third_party_packages") and not (hit and time.time() - hit[0] < APPS_TTL):
        ok, apps = _bounded(dev.third_party_packages, ACCOUNT_WAIT)
        if ok:
            jobs.remember_apps(key, apps)
            hit = cache[key]
    if not hit:
        return "" if (dev is None or not hasattr(dev, "third_party_packages")) else "not known yet (ask with phone_check kind app_installed)"
    shown = ", ".join(hit[1][:APPS_SHOWN]) + (f" and {len(hit[1]) - APPS_SHOWN} more" if len(hit[1]) > APPS_SHOWN else "") if hit[1] else "none"
    return shown + ("" if awake else " (as last seen, the phone is asleep)")


def describe(db, jobs) -> list[dict]:
    """Every phone with its bot, capability, live status, Google account and known-not-to-run apps."""
    bad: dict[str, list[str]] = {}
    for f in crash_facts(db):
        bad.setdefault(f["phone"], []).append(f["package"])
    out = []
    lc = jobs.lifecycle
    power = (lc.info() if lc else {"power": "on"}).get("power", "on")
    out.append({"key": "emulator", "label": "Shared emulator", "bot": "any normal bot, or Master", "status": POWER_TEXT.get(power, power), "can": EMULATOR,
                "account": google_account_fact(jobs, "emulator", jobs.device, lc), "known_bad": bad.get("emulator", []),
                "apps": installed_apps_fact(jobs, "emulator", jobs.device, lc)})
    adb = getattr(jobs, "adb_phones", None)
    for name, (vdev, vlc) in sorted(jobs.virtuals.items()):
        bot = next((b for b in db.list_bots() if b["kind"] == "virtual" and b["phone_id"] == name), None)
        extra = vlc is None and adb is not None and name in adb.config
        if extra:
            up = adb.online(name)
            status, can = ("ready" if up else "OFFLINE (adb cannot reach it)"), ADB.format(serial=adb.serial(name), play=("Google Play is installed." if adb.has_play(name) else "There is no Google Play on it: apps come as APK files."))
            vlc_for_facts = None
            acct = google_account_fact(jobs, name, vdev, vlc_for_facts) if up else "unknown (the phone is offline)"
            apps = installed_apps_fact(jobs, name, vdev, vlc_for_facts) if up else ""
        else:
            vp = (vlc.info() if vlc else {"power": "unavailable"}).get("power", "unavailable")
            status, can = POWER_TEXT.get(vp, vp), GAMES
            acct, apps = google_account_fact(jobs, name, vdev, vlc), installed_apps_fact(jobs, name, vdev, vlc)
        out.append({"key": name, "label": (bot or {}).get("name") or f"{name} phone", "bot": (bot or {}).get("name"), "status": status, "can": can,
                    "account": acct, "known_bad": bad.get(name, []), "apps": apps})
    online = {p["id"] for p in (jobs.phones.listing() if jobs.phones else [])}
    for b in db.list_bots():
        if b["kind"] == "phone":
            out.append({"key": f"phone:{b['phone_id']}", "label": b["name"], "bot": b["name"], "status": "connected" if b["phone_id"] in online else "OFFLINE (the app is not connected)",
                        "can": REAL, "account": "unknown (ask with phone_check kind account)", "known_bad": bad.get(f"phone:{b['phone_id']}", [])})
    good: dict[str, list[str]] = {}
    for f in db.launch_facts():  # apps the agent opened on a phone and saw stay up: the next "open it" goes straight there
        if f["ok"]:
            good.setdefault(f["phone"], []).append(f["package"])
    for e in out:
        e["runs"] = good.get(e["key"], [])
    return out


def render(entries: list[dict], viewer_key: str | None = None, master: bool = False) -> str:
    """The directory as text for a bot's context. `viewer_key` marks the phone the reading bot itself works on."""
    lines = ["Phones you can know about (what each can and cannot do, and how it is right now). Never offer or promise something the phone it would run on cannot do:"]
    for e in entries:
        mark = " <- this is YOUR phone" if viewer_key == e["key"] else ""
        bad = f" Known NOT to run here (crashed during a task): {', '.join(e['known_bad'])}." if e["known_bad"] else ""
        bad += f" Known to run here (opened and stayed up): {', '.join(e['runs'])}." if e.get("runs") else ""
        acct = f" Google account: {e['account']}." if e.get("account") else ""
        apps = f" Installed apps: {e['apps']}." if e.get("apps") else ""
        lines.append(f"- {e['label']} [{e['status']}]{mark}: {e['can']}{acct}{apps}{bad}" + (f" Its bot: {e['bot']}." if e["bot"] and e["key"] != "emulator" else ""))
    lines.append("Game accounts: no phone plays Supercell games (Clash of Clans, Clash Royale, Brawl Stars, Hay Day, Boom Beach, Squad Busters), Pokemon GO or any game in the owner's policy list: "
                 "their publishers ban accounts that use bots, so the phone refuses every tap, scroll and key in them. The agent may only open them, check they are installed, move them between phones "
                 "and say what the screen shows; the owner plays them with Take control on Cloud phone 2 (or any extra adb phone).")
    if master:
        lines.append(
            "How to route a phone task: (1) list the candidate phones, and use phone_check with \"on\" (emulator, games, or a real phone's bot name) to see where the app is installed; "
            "(2) prefer the phone that already has the app (see its Installed apps) and is not listed as Known NOT to run it; (3) installs from the Play Store only on the emulator (queue it with mobile_task); "
            "(4) hand the task to a phone's bot with delegate_task (Cloud phone 2 and a real phone have their own bot); run it yourself on the emulator with mobile_task; "
            "(5) an app that is installed on the emulator but is wanted on Cloud phone 2 is moved with move_app (emulator -> games), never re-downloaded. When an app crashes on a phone, "
            "THE SYSTEM ITSELF tries the owner's other phones in order (emulator, Cloud phone 2, extra adb phones, the owner's own phone), copying the app where needed and checking that it "
            "really stays up, with no question to the owner, and it posts the result in the chat where they asked: never ask 'want me to try Cloud phone 2?' for these steps and do not "
            "start that chain yourself; "
            "(6) when no phone can do it, say so plainly: what was tried where, why it cannot work, and the ONE thing that would unblock it (an APK on Cloud phone 2, "
            "the Mobile Agent app connected on the owner's phone). Do not invent options.\n"
            "Google sign-in is never copied between phones. When a phone that CAN have a Google account (the emulator, the owner's phone) needs it, queue a sign-in task on THAT phone's bot "
            "(Settings > Accounts > Add account > Google; the password goes in only with type_secret from the vault, the owner approving each use; 2-step verification is a handoff to the owner). "
            "Cloud phone 2 has no Google services and can never sign in: say so, do not offer it.")
    else:
        lines.append("If an app crashes on your phone, or is listed above as Known NOT to run on it, the system itself tries the owner's other phones and tells them where it ran, in this chat: "
                     "still queue the owner's task with mobile_task as usual, and never tell them to ask Master or to try another phone for that. "
                     "If your phone cannot do some other task but another one could, name that phone and tell the owner to ask Master (it can hand tasks to any phone). Do not promise to do it yourself.")
    return "\n".join(lines)


def runs_on(db, package: str) -> list[str]:
    """Phones where this app was opened and stayed up (newest first): the agent goes there first."""
    return [f["phone"] for f in db.launch_facts(package) if f["ok"]]


def job_phone_key(job: dict) -> str:
    """The phone a job ran on, as the directory names it: emulator, games (or another virtual name), or phone:<id>."""
    t = job.get("target") or ""
    return "emulator" if not t else t.split(":", 1)[1] if t.startswith("virtual:") else t


def known_bad(db, key: str, package: str) -> bool:
    return any(f["phone"] == key and f["package"] == package for f in crash_facts(db))
