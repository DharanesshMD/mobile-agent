"""Automatic escalation: an app that cannot run on one phone is tried on the owner's other phones, in order, with no question at each step.

Order: the phones where the agent already saw the app stay up, then the shared emulator, Cloud phone 2, the extra adb phones, and last the owner's own phone (only while its app is
connected, and it installs from its own Play Store). Moving an app between the owner's own phones and opening it are not destructive and are undone by closing the app, so they need no
permission. Signing in, entering secrets, installing from an untrusted source, deleting and buying still ask the owner, and nothing here ever plays a game.

The chain is code, not a model decision: it is at most CHAIN_MAX phones long, it never repeats a phone, it skips phones where the app is known not to run, it checks that the app really
stays up (in front, no crash for LAUNCH_WAIT_S seconds, a described screenshot) before saying so, and it remembers what worked (db.launch_facts) so the next "open it" goes straight there.
"""
import re
import threading
import time
from dataclasses import dataclass, field

from agent import names
from agent import gamepolicy
from agent import phones
from agent.checks import _norm
from agent.config import CHAIN_MAX, WORKER_MODEL
from agent.moveapp import move_app

LAUNCH_WAIT_S = 20  # the app must stay in front, without a crash, for this long after it is opened
LAUNCH_POLL_S = 2
LANE_WAIT_S = 120  # how long to wait for a phone that is busy with another task before skipping it

OPEN_RE = re.compile(r"\b(open|launch|start|run|play|use)\b", re.I)
SKIP_RE = re.compile(r"\b(install|uninstall|delete|remove|update|download|sign ?in|log ?in|purchase|buy)\b", re.I)
NAME_RE = re.compile(r"\b(?:open|launch|start|run|play|use)\s+(?:the\s+)?(.+?)(?:\s+(?:app|game|and|then|on|in|to|so)\b|[.,;:!?](?:\s|$)|$)", re.I)


SIMPLE_OPEN = re.compile(r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:open|launch|start|run)\s+(?:the\s+)?[\w'’&. -]{2,40}?\s*(?:app|game)?\s*[.!?]*\s*$", re.I)


def package_in_goal(packages: list[str], goal: str) -> str | None:
    """Which of these packages does an "open ..." goal name? (Only open-type goals: never an install, a sign-in or a delete.)"""
    if not OPEN_RE.search(goal) or SKIP_RE.search(goal):
        return None
    g = _norm(goal)
    for p in packages:
        seg = _norm(p.split(".")[-1])
        if len(seg) >= 5 and seg in g:
            return p
    return None


SMALL_WORDS = {"of", "the", "and", "a", "an", "in", "on", "for", "to", "vs"}


def _nice(name: str) -> str:
    """"clash of clans" -> "Clash of Clans"; a name the owner already capitalised is kept."""
    if name != name.lower():
        return name
    return " ".join(w if (i and w in SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(name.split()))


def _sentence(text: str) -> str:
    """One sentence that ends with exactly one full stop, question mark or exclamation mark."""
    t = " ".join(text.split())
    return t if t[-1:] in ".?!" else t + "."


def app_name(goal: str, package: str) -> str:
    """The name the owner used for the app (for the messages), else the last part of its package."""
    seg = package.split(".")[-1]
    m = NAME_RE.search(re.sub(r"\s*\([\w.]+\)", "", goal or ""))  # the model often writes "Clash of Clans (com.supercell.clashofclans)": keep only the name
    name = m.group(1).strip(" \"'") if m else ""
    if name and (_norm(seg) in _norm(name) or _norm(name) in _norm(package)):
        return _nice(name)
    return seg.title()


def verify_launch(dev, package: str, wait: float = LAUNCH_WAIT_S, poll: float = LAUNCH_POLL_S, sleep=time.sleep) -> tuple[bool, str]:
    """Open the app and make sure it really stays up: it is the app in front at the end, and no crash of it appears in the crash log for `wait` seconds."""
    def crashes() -> list[str]:
        try:
            return [pid for pkg, pid in dev.crashes() if pkg == package] if hasattr(dev, "crashes") else []
        except Exception:
            return []
    before = set(crashes())
    dev.open_app(package)
    waited, front = 0.0, ""
    while waited < wait:
        sleep(poll)
        waited += poll
        new = [pid for pid in crashes() if pid not in before]
        if new:
            return False, f"it crashed within {round(waited)} s of opening"
        try:
            front = dev.foreground()
        except Exception:
            front = ""
    if front != package:
        return False, f"it did not stay in front ({front or 'nothing'} was showing)"
    return True, ""


@dataclass
class Chain:
    asker: str  # the bot whose chat gets the answer
    package: str
    app: str
    goal: str
    failed: str  # the phone key where it did not run
    tried: list = field(default_factory=list)  # {"key", "label", "reason", "attempted"}
    moved: bool = False


class Escalator:
    def __init__(self, orch, sleep=time.sleep, launch_wait: float = LAUNCH_WAIT_S, lane_wait: float = LANE_WAIT_S, background: bool = True):
        self.orch, self.sleep, self.launch_wait, self.lane_wait, self.background = orch, sleep, launch_wait, lane_wait, background
        self._active: set[tuple[str, str]] = set()
        self._jobs: dict[str, tuple[Chain, str]] = {}  # job id of a task started on the owner's real phone -> (chain, phone key)
        self._lock = threading.Lock()
        self.describe = self._describe_screen  # replaceable in tests

    # --- entry points -----------------------------------------------------------------------------------------------
    def owns(self, job_id: str) -> bool:
        return job_id in self._jobs

    def known_bad_package(self, phone_key: str, goal: str) -> str | None:
        """The app an "open ..." goal names, if it is known not to run on this phone (so the task goes elsewhere at once)."""
        bad = [f["package"] for f in phones.crash_facts(self.orch.db) if f["phone"] == phone_key]
        return package_in_goal(bad, goal)

    def start(self, asker: str, package: str, goal: str, failed: str, intro: str = "") -> bool:
        """Start the chain for an app that did not run on `failed`. False if one is already running for this bot and app."""
        with self._lock:
            if (asker, package) in self._active:
                return False
            self._active.add((asker, package))
        chain = Chain(asker, package, app_name(goal, package), goal, failed)
        if intro:
            self.orch.post(asker, intro)
        if self.background:
            threading.Thread(target=self._run, args=(chain,), daemon=True, name="escalate").start()
        else:
            self._run(chain)
        return True

    # --- the chain ----------------------------------------------------------------------------------------------------
    def _run(self, chain: Chain) -> None:
        try:
            self._advance(chain)
        except Exception as e:  # the owner always hears the outcome
            self._done(chain)
            self.orch.post(chain.asker, f"I could not finish trying other phones for {chain.app} ({type(e).__name__}). It did not run on {self._label(chain.failed)}.")

    def _done(self, chain: Chain) -> None:
        with self._lock:
            self._active.discard((chain.asker, chain.package))

    def _keys(self) -> list[str]:
        jobs, db = self.orch.jobs, self.orch.db
        games = ["games"] if "games" in jobs.virtuals else []
        extras = sorted(n for n in jobs.virtuals if n != "games")
        real = [f"phone:{b['phone_id']}" for b in db.list_bots() if b["kind"] == "phone"]
        return ["emulator", *games, *extras, *real]

    def order(self, chain: Chain) -> list[str]:
        keys = self._keys()
        good = [k for k in phones.runs_on(self.orch.db, chain.package) if k in keys]  # the winner of an earlier time goes first
        done = {t["key"] for t in chain.tried}
        return [k for k in good + [k for k in keys if k not in good] if k != chain.failed and k not in done]

    def _label(self, key: str) -> str:
        if key == "emulator":
            return names.SHARED
        if key.startswith("phone:"):
            b = next((x for x in self.orch.db.list_bots() if x["kind"] == "phone" and x["phone_id"] == key.split(":", 1)[1]), None)
            return (b or {}).get("name") or "that phone"
        return names.SECOND if key == "games" else f"the {key} phone"

    def _lane(self, key: str) -> str:
        return "" if key == "emulator" else key if key.startswith("phone:") else f"virtual:{key}"

    def _pair(self, key: str) -> tuple:
        """(device, lifecycle) of a phone, or (None, reason)."""
        from agent.remote import RemoteDevice
        jobs = self.orch.jobs
        if key == "emulator":
            return jobs.device, jobs.lifecycle
        if key.startswith("phone:"):
            pid = jobs.phones.pick(key.split(":", 1)[1]) if jobs.phones else None
            return (RemoteDevice(jobs.phones, pid), None) if pid else (None, "your phone is not connected (open the Mobile Agent app on it)")
        dev, lc = jobs.virtuals.get(key, (None, None))
        if dev is None:
            return None, "it is not set up on this computer"
        adb = getattr(jobs, "adb_phones", None)
        if lc is None and adb is not None and key in adb.config and not adb.online(key, fresh=True):
            return None, f"adb cannot reach it ({adb.serial(key)})"
        return dev, lc

    def _advance(self, chain: Chain) -> None:
        attempts = sum(1 for t in chain.tried if t["attempted"])
        for key in self.order(chain):
            if attempts >= CHAIN_MAX:
                break
            label = self._label(key)
            if phones.known_bad(self.orch.db, key, chain.package):
                chain.tried.append({"key": key, "label": label, "reason": "it crashed there before", "attempted": False})
                continue
            dev, lc = self._pair(key)
            if dev is None:
                chain.tried.append({"key": key, "label": label, "reason": str(lc), "attempted": False})
                continue
            attempts += 1
            outcome, detail = self._attempt(chain, key, label, dev, lc)
            if outcome == "pending":
                return  # a task on the owner's phone is running; its end continues the chain (job_done)
            if outcome == "ok":
                return self._succeed(chain, key, label, detail)
            chain.tried.append({"key": key, "label": label, "reason": detail, "attempted": True})
        self._give_up(chain)

    def _attempt(self, chain: Chain, key: str, label: str, dev, lc) -> tuple[str, str]:
        jobs, db = self.orch.jobs, self.orch.db
        lane, end = self._lane(key), time.time() + self.lane_wait
        while jobs.busy(lane) and time.time() < end:  # never touch a phone in the middle of another task
            self.sleep(2)
        if jobs.busy(lane):
            return "fail", "it was busy with another task"
        try:
            if lc is not None and not lc.ensure_healthy(180):
                return "fail", "it did not wake up in time"
            installed = chain.package in dev.packages()
            if not installed:
                if key.startswith("phone:"):
                    return self._install_on_real(chain, key, label)
                src = self._source(chain, key)
                if not src:
                    return "fail", "the app is not installed there and no other phone has it to copy"
                self.orch.post(chain.asker, f"Moving {chain.app} to {label}…")
                move_app(src, dev, chain.package)
                chain.moved = True
                if hasattr(dev, "third_party_packages"):
                    jobs.remember_apps(key, dev.third_party_packages())
            ok, reason = verify_launch(dev, chain.package, wait=self.launch_wait, sleep=self.sleep)
        except Exception as e:
            return "fail", (str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {str(e)[:120]}")
        db.set_launch_fact(key, chain.package, ok, reason)  # known to run (go there first next time) or known not to (skip it)
        return ("ok", self.describe(dev)) if ok else ("fail", reason)

    def _source(self, chain: Chain, dest: str):
        """A phone (an adb one, awake) that has the app installed, to copy it from: the phone it failed on first."""
        for key in [chain.failed, *self._keys()]:
            if key == dest or key.startswith("phone:"):
                continue
            dev, lc = self._pair(key)
            if dev is None or not hasattr(dev, "apk_paths"):
                continue
            try:
                if lc is not None and not lc.ensure_healthy(90):
                    continue
                if chain.package in dev.packages():
                    return dev
            except Exception:
                continue
        return None

    def _install_on_real(self, chain: Chain, key: str, label: str) -> tuple[str, str]:
        """The owner's own phone installs from its own Play Store, through its bot, then opens the app and stays on its first screen."""
        bot = next((b for b in self.orch.db.list_bots() if b["kind"] == "phone" and f"phone:{b['phone_id']}" == key), None)
        if not bot:
            return "fail", "its bot is missing"
        goal = (f"Open the Play Store on this phone, find {chain.app} (the official app) and install it if it is not installed, then open it and stay on its first screen. "
                "Do not sign in, buy anything or play. Report what the screen shows.")
        self.orch.post(chain.asker, f"Asking {label} to get {chain.app} from its own Play Store…")
        self.orch.post(bot["id"], f"{self.orch.db.get_bot(chain.asker)['name']} asked you to open {chain.app} here, because it does not run on {self._label(chain.failed)}.", "bot_note", {"with": chain.asker})
        job = self.orch.jobs.submit(bot["id"], goal, verify={"kind": "foreground", "query": chain.package.split(".")[-1]})
        self._jobs[job["id"]] = (chain, key)
        return "pending", job["id"]

    def job_done(self, job: dict) -> None:
        """A task the chain started on the owner's phone ended: carry on (success) or move to the next phone."""
        chain, key = self._jobs.pop(job["id"])
        label, ok = self._label(key), job["status"] == "done"
        self.orch.db.set_launch_fact(key, chain.package, ok, (job["result"] or job["status"])[:200])
        if ok:
            return self._succeed(chain, key, label, job["result"] or "")
        chain.tried.append({"key": key, "label": label, "reason": (job["result"] or job["status"])[:160], "attempted": True})
        threading.Thread(target=self._run, args=(chain,), daemon=True, name="escalate").start() if self.background else self._run(chain)

    # --- the owner hears the outcome -----------------------------------------------------------------------------------
    def _succeed(self, chain: Chain, key: str, label: str, desc: str) -> None:
        self._done(chain)
        earlier = [t for t in chain.tried if t["attempted"]]
        tried = f" (I tried {', '.join(t['label'] + ': ' + t['reason'] for t in earlier)} first)" if earlier else ""
        copied = " I copied the app over, only the app: its data and sign-in stayed where they were." if chain.moved else ""
        text = (f"{chain.app} doesn't run on {self._label(chain.failed)}, so I ran it on {label}{tried}.{copied} It's open now and shows: {_sentence(desc)} "
                + ("I only opened it: nothing was tapped. I won't play it for you: its publisher bans accounts that use bots. To play it yourself, use Take control on that phone."
                   if gamepolicy.blocked(chain.package) else "I only opened it: nothing was tapped, and I won't play unless you ask."))
        self.orch.post(chain.asker, text)
        self.orch.db.memory_add(chain.asker, f"{chain.app} runs on {label}, not on {self._label(chain.failed)}")

    def _give_up(self, chain: Chain) -> None:
        self._done(chain)
        real = [b for b in self.orch.db.list_bots() if b["kind"] == "phone"]
        tried = "; ".join(f"{t['label']}: {t['reason']}" for t in chain.tried) or "no other phone is set up"
        if not real:
            unblock = "connect another phone with the Mobile Agent app: it installs from its own Play Store, which is the one place left to try"
        elif any(t["key"].startswith("phone:") and not t["attempted"] for t in chain.tried):
            unblock = "open the Mobile Agent app on your phone (it is not connected right now) and ask again"
        else:
            unblock = "a different kind of device: none of your phones can run it"
        self.orch.post(chain.asker, f"I couldn't get {chain.app} to run on any of your phones. It doesn't run on {self._label(chain.failed)}. What I tried: {tried}. What would unblock it: {unblock}.")
        if getattr(self.orch, "web_in_chain", False):  # the last step: look for other ways on the web, and let the owner decide
            threading.Thread(target=self.orch.web_options, args=(chain.asker, f"play {chain.app} online or on PC", f"another way to use {chain.app}",
                                                                 f"None of my phones can run {chain.app}. Here are other ways, with their risks:"), daemon=True, name="web-options").start()

    def _describe_screen(self, dev) -> str:
        """One short sentence about what the phone shows now (one call of the worker model on a screenshot)."""
        try:
            png = dev.screenshot()
            text = self.orch.model_factory(WORKER_MODEL).complete(
                "You describe what is on a phone screen, plainly and briefly.",
                "In one short sentence say what this phone screen shows: the app, and what it asks or displays. Describe only what is visible. Reply with the sentence only.", png, 120)
            line = str(text).strip().splitlines()[0].strip().strip('"') if str(text).strip() else ""
            return line[:220] or "a screen I could not read"
        except Exception:
            return "a screen I could not describe"
