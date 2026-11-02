"""Background phone jobs: a queue per phone that respects dependencies, owner takeover, and worker gates turned into chat cards.

Every phone is its own lane: the shared emulator (lane ""), each virtual phone ("virtual:games") and each real phone ("phone:<id>"). A lane runs one job at a
time in the order they were asked, and lanes run in parallel, so a long game task never blocks the other bots. A job with `after` waits until that job is
done (in any lane) and is skipped if it did not succeed. A job waiting for the owner gives the phone up after OWNER_WAIT_MIN, owner control of the phone ends after HOLD_IDLE_MIN without input,
and a job that hits a dead phone restarts it and runs once more, so the queue cannot freeze on one stuck task or one dead emulator.
"""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from agent import cloudtools, report as report_mod, reportsave
from agent.artifacts import Artifacts
from agent.checks import run_check
from agent.config import HOLD_IDLE_MIN, OWNER_WAIT_MIN, WORKER_MODEL
from agent.db import Database
from agent.graph import build_graph, run as run_graph
from agent.hub import Hub
from agent.remote import RemoteDevice
from agent.vault import SecretUse, Vault

ACTIVE = ("queued", "running", "waiting")
PHONE_ERROR_WORDS = ("adb", "device offline", "device not found", "no devices", "uiautomator", "connection refused", "timed out")


def is_phone_error(e: Exception) -> bool:
    """Did the phone itself fail (hung, offline, adb died), as opposed to a bug or a bad action? Those are worth a restart and one more try."""
    if isinstance(e, (subprocess.TimeoutExpired, subprocess.CalledProcessError, ConnectionError, TimeoutError, OSError)):
        return True
    return not isinstance(e, (ValueError, KeyError, TypeError)) and any(w in str(e).lower() for w in PHONE_ERROR_WORDS)


REPORT_GOAL = {
    "link": ("\n\nLINK CHECK (observe only, on a throwaway phone): open the address with open_url in Chrome (never tap links on the page, never type, never download). "
             "Wait for it to load. Read the address bar for the FINAL address and say whether it changed from the one you were given (a redirect). Note whether Chrome shows a warning page "
             "(for example 'Deceptive site ahead', 'Dangerous site', 'malware'), whether a password, card or personal-data form is visible, and what the page claims to be. "
             "If Chrome first shows a welcome or sign-in screen, tap 'Stay signed out' (or No thanks) and never capture that screen. Use capture for 2 or 3 pictures (the page after it loads, the warning or the form if there is one). Then record verdict: level harmful (a Chrome warning page, "
             "or a sign-in or payment form that imitates another brand, or an obvious scam), suspicious (urgent pressure, a look-alike address, an unexpected redirect, a request for personal data) "
             "or nothing_found; the reasons; what you checked; what you did NOT check. Never use the word safe. Finish with done: one sentence with the verdict."),
    "matrix": ("\n\nLOOK-AND-FEEL CHECK: open the app or Settings page the goal names. FIRST set the baseline with set_device (dark_mode off, font_scale normal, size normal) so the first picture is a true 'Normal'. For each state the goal lists (dark mode, large text, another language, a small and a big screen), "
               "apply it with set_device (language needs the app's package; if it says the app does not support it, say so and continue), then capture one picture of the SAME screen with the state as its title. "
               "Do not undo anything yourself: the phone is put back when the task ends. Finish with done: what you could SEE differ (text cut off, overlapping, unreadable) and what you could not check."),
    "guide": ("\n\nSTEP-BY-STEP GUIDE: do the task one step at a time. After each meaningful step use capture with a short imperative title ('Open Settings') and a note in plain words for a beginner "
              "(what to tap and what they will see). 5 to 12 steps. Never put passwords, personal data or the owner's account name in a title or note. The phone's dark mode, text size and screen timeout are put back when the task ends, so do not add 'undo' steps unless the owner asked for them. Finish with done: one sentence."),
    "plain": "\n\nREPORT: use capture for the screens that matter, with a title and a caption, then finish with done.",
}


class JobRun:
    def __init__(self, job: dict, status_msg_id: int):
        self.job_id, self.bot_id, self.goal, self.after, self.verify = job["id"], job["bot_id"], job["goal"], job.get("after"), job.get("verify")
        self.report = job.get("report") or ""  # link | matrix | guide | plain: what the task collects and how its report is built (agent/report.py)
        self.files = list(job.get("files") or [])  # ids of pictures the owner shared that this task needs on the phone
        self.target = job.get("target") or ""  # "" is the shared emulator, "virtual:games" Cloud phone 2, "phone:<id>" the owner's real phone
        self.lane = self.target  # one running job per lane
        self.timed_out = False
        self.status_msg_id = status_msg_id
        self.cancel = threading.Event()
        self.resume = threading.Event()
        self.resume_value: dict = {}
        self.pending_item: str | None = None
        self.notes: list[str] = []  # owner updates sent while the job runs; the worker takes them at its next step


class JobManager:
    def __init__(self, db: Database, hub: Hub, device, model_factory, data_dir: str | Path = "data",
                 settle: float = 0.4, max_steps: int = 25, external_busy=None, vault: Vault | None = None,
                 owner_wait_s: float = OWNER_WAIT_MIN * 60, hold_idle_s: float = HOLD_IDLE_MIN * 60, phone_retries: int = 1):
        self.db, self.hub, self.device, self.model_factory = db, hub, device, model_factory
        self.owner_wait_s, self.hold_idle_s, self.phone_retries = owner_wait_s, hold_idle_s, phone_retries
        self.data_dir, self.settle, self.max_steps = Path(data_dir), settle, max_steps
        self.external_busy = external_busy or (lambda: False)
        self.vault = vault
        self.lifecycle = None  # phone power (sleep and wake), attached by the app
        self.adb_phones = None  # extra adb phones (agent/adbphones.py), attached by the app
        self.phones = None  # real phones connected through the app (agent/remote.py), attached by the app
        self.app_cache: dict = self._load_app_cache()  # phone key -> (when, user-installed packages)
        self.virtuals: dict[str, tuple] = {}  # name -> (Device, Lifecycle) of each virtual phone (agent/virtual.py), attached by the app
        self.orch = None  # set by the app after construction
        self.graph = build_graph()
        self.runs: dict[str, JobRun] = {}
        self._running: dict[str, JobRun] = {}  # lane -> its running job
        self._pending: list[JobRun] = []  # queued jobs in the order they were asked, all lanes
        self._cv = threading.Condition()
        self.held_lanes: set[str] = set()  # lanes whose phone the owner is driving ("" is the shared emulator, "virtual:games" Cloud phone 2, ...)
        self._hold_at: dict[str, float] = {}  # lane -> when the owner last did something there (the idle release)
        self._threads: dict[str, threading.Thread] = {}
        self._started = False

    def _load_app_cache(self) -> dict:
        try:
            return {k: (float(v[0]), list(v[1])) for k, v in json.loads((self.data_dir / "app_cache.json").read_text()).items()}
        except Exception:
            return {}

    def remember_apps(self, key: str, apps: list[str]) -> None:
        """The apps last seen on a phone (kept across restarts), so a phone that is asleep can still be chosen for the app it has."""
        self.app_cache[key] = (time.time(), list(apps))
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "app_cache.json").write_text(json.dumps(self.app_cache))
        except OSError:
            pass

    # --- lifecycle -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.recover()
        self._started = True
        for lane in {"", *(jr.lane for jr in self._pending)}:
            self._ensure_lane(lane)

    def _ensure_lane(self, lane: str) -> None:
        """Each lane has its own worker thread, started when the lane first has work."""
        with self._cv:
            t = self._threads.get(lane)
            if not self._started or (t and t.is_alive()):
                return
            self._threads[lane] = t = threading.Thread(target=self._loop, args=(lane,), daemon=True, name=f"phone-jobs{':' + lane if lane else ''}")
        t.start()

    @property
    def current(self) -> "JobRun | None":
        """The job running on the shared emulator."""
        return self._running.get("")

    def running(self) -> list["JobRun"]:
        return list(self._running.values())

    @property
    def held(self) -> bool:
        """The owner has control of the shared emulator."""
        return "" in self.held_lanes

    @held.setter
    def held(self, value: bool) -> None:
        (self.held_lanes.add if value else self.held_lanes.discard)("")

    def is_held(self, lane: str = "") -> bool:
        return lane in self.held_lanes

    def lane_of(self, bot_id: str) -> str:
        """Which phone a bot's jobs run on: Cloud phone 2's bot and a real phone's bot have their own, every other bot uses the shared emulator."""
        bot = self.db.get_bot(bot_id) or {}
        if bot.get("kind") == "phone" and bot.get("phone_id"):
            return f"phone:{bot['phone_id']}"
        if bot.get("kind") == "virtual" and bot.get("phone_id"):
            return f"virtual:{bot['phone_id']}"
        return ""

    def recover(self) -> None:
        """After a restart nothing is really running: fail the jobs that were running or waiting and close their cards.
        Jobs that never started are put back in the queue."""
        for j in self.db._q("select id, bot_id, goal from jobs where status in ('running','waiting')"):
            self.db.update_job(j["id"], status="failed", ended=time.time(), result="interrupted by a restart")
            if self.db.get_bot(j["bot_id"]):  # say so in the chat, or the history would still read "on it" for a task that is dead
                msg = self.db.add_message(j["bot_id"], "assistant", "text", f"I was interrupted by a restart and did not finish: {j['goal']}\nAsk me again and I'll start it over.")
                self.hub.publish(j["bot_id"], {"type": "message", "message": msg})
        for it in self.db.open_items():
            if it["job_id"]:
                self.db.close_item(it["id"], "cancelled")
                if it["message_id"]:
                    m = self.db.get_message(it["message_id"])
                    if m:
                        self.db.update_message(it["message_id"], meta={**m["meta"], "status": "cancelled"})
        for m in self.db._q("select id from messages where kind='status'"):
            self.db.delete_message(m["id"])
        for j in self.db._q("select * from jobs where status='queued' order by started"):
            if not self.db.get_bot(j["bot_id"]):
                self.db.update_job(j["id"], status="cancelled", ended=time.time())
                continue
            msg = self.db.add_message(j["bot_id"], "assistant", "status", "Waiting for the phone…", {"job_id": j["id"]})
            self.runs[j["id"]] = jr = JobRun(j, msg["id"])
            self._pending.append(jr)
        self._announce_queue()

    # --- queries -------------------------------------------------------------------------------------------------
    def active_for(self, bot_id: str) -> dict | None:
        rows = self.db._q(f"select * from jobs where bot_id=? and status in ({','.join('?' * len(ACTIVE))}) order by started limit 1",
                          (bot_id, *ACTIVE))
        return rows[0] if rows else None

    def busy(self, lane: str | None = None) -> bool:
        """Is a phone busy? With a lane, that phone only; without, any phone."""
        with self._cv:
            if lane is None:
                return bool(self._running) or bool(self._pending)
            return lane in self._running or any(r.lane == lane for r in self._pending)

    def busy_for_bot(self, bot_id: str) -> bool:
        return self.busy(self.lane_of(bot_id))

    def pair(self, lane: str) -> tuple:
        """(device, lifecycle) of a lane's phone: the shared emulator, or a virtual phone."""
        if lane.startswith("virtual:"):
            return self.virtuals.get(lane.split(":", 1)[1], (None, None))
        if lane.startswith("phone:"):
            return None, None  # a real phone is reached through the hub, with no power to manage
        return self.device, self.lifecycle

    def state(self, lane: str = "") -> dict:
        """The Mobile panel's view of one phone (the shared emulator by default, or `virtual:games`), plus a summary of every lane."""
        with self._cv:
            cur = self._running.get(lane)
            queue = [r for r in self._pending if r.lane == lane]
        job = self.db.get_job(cur.job_id) if cur else None
        held = lane in self.held_lanes
        if held:
            state = "control"
        elif job:
            state = "waiting" if job["status"] == "waiting" else "working"
        else:
            state = "ready"
        device, lifecycle = self.pair(lane)
        power = lifecycle.info() if lifecycle else {"power": "on"}
        if lifecycle is None and self.adb_phones is not None and lane.startswith("virtual:") and not self.adb_phones.online(lane.split(":", 1)[1]):
            power = {"power": "off", "detail": "adb cannot reach this phone"}  # an extra adb phone has no power to manage: it is simply there, or not
        try:  # never ask a sleeping phone anything: its adb connection would hang
            orientation = device.orientation() if device is not None and power["power"] == "on" and hasattr(device, "orientation") else "portrait"
        except Exception:
            orientation = "portrait"
        lanes = {f"virtual:{name}": {"power": (lc.info() if lc else {"power": "on"})["power"], "busy": self.busy(f"virtual:{name}")} for name, (_, lc) in self.virtuals.items()}
        return {"phones": self.phones.listing() if self.phones else [], "state": state, "held": held, "job": job, "queued": len(queue),
                "queue": [{"id": r.job_id, "bot_id": r.bot_id, "goal": r.goal[:100], "after": r.after} for r in queue], "orientation": orientation, "phone": power,
                "lanes": lanes}

    # --- submit and cancel ---------------------------------------------------------------------------------------
    def submit(self, bot_id: str, goal: str, after: str | None = None, verify: dict | None = None, routine: str | None = None, files: list[str] | None = None,
               report: str | None = None) -> dict:
        """Queue a phone job. With `after` (a job id) it waits for that job to finish successfully and is skipped if it does not.
        With `verify` (a phone check) the worker's "done" only counts if the phone really shows it."""
        target = self.lane_of(bot_id)  # a phone's bot only ever works on that phone, and no other bot ever uses it
        job = self.db.create_job(bot_id, goal, after, verify, routine, target, files, report)
        msg = self.db.add_message(bot_id, "assistant", "status", "Waiting for the phone…" if self.busy(target) or after else "Starting…", {"job_id": job["id"]})
        self.hub.publish(bot_id, {"type": "message", "message": msg})
        jr = JobRun(job, msg["id"])
        self.runs[job["id"]] = jr
        with self._cv:
            self._pending.append(jr)
            self._cv.notify_all()
        self._ensure_lane(jr.lane)
        self._announce_queue()
        return job

    def cancel(self, job_id: str) -> bool:
        jr = self.runs.get(job_id)
        if not jr:
            return False
        jr.cancel.set()
        jr.resume_value = {"cancelled": True}
        jr.resume.set()
        with self._cv:
            queued = jr in self._pending
            if queued:
                self._pending.remove(jr)
            self._cv.notify_all()
        job = self.db.get_job(job_id)
        if job and job["status"] in ("running", "waiting"):
            self._set_status(jr, "Stopping after the current step…")
        if job and job["status"] == "queued":  # never started: close it here
            self.db.update_job(job_id, status="cancelled", ended=time.time())
            self._drop_status(jr)
            self.runs.pop(job_id, None)
            self.orch.job_finished(jr.bot_id, self.db.get_job(job_id))
            self._announce_queue()
        return True

    def steer(self, job_id: str, note: str) -> bool:
        """Pass an owner update to a queued or running job. The worker sees it before its next step."""
        jr = self.runs.get(job_id)
        note = " ".join(note.split())[:500]
        if not jr or not note:
            return False
        jr.notes.append(note)
        jr.goal = f"{jr.goal}\nOwner update: {note}"
        self.db.update_job(job_id, goal=jr.goal)
        return True

    # --- owner control of the phone ------------------------------------------------------------------------------
    def take_control(self, lane: str = "") -> None:
        """The owner drives a phone: its lane pauses at the agent's next step (the other phones carry on)."""
        lifecycle = self.pair(lane)[1]
        if lifecycle:
            lifecycle.wake()
        with self._cv:
            self.held_lanes.add(lane)
            self._hold_at[lane] = time.time()

    def release_control(self, lane: str = "") -> None:
        with self._cv:
            self.held_lanes.discard(lane)
            self._cv.notify_all()

    def owner_input(self, lane: str = "") -> None:
        """The owner just did something on this phone: control is not forgotten."""
        self._hold_at[lane] = time.time()

    def _expire_hold(self, lane: str = "") -> None:
        """Owner control that is forgotten (no input for HOLD_IDLE_MIN) ends by itself, or every queued task would wait for ever."""
        if lane not in self.held_lanes:
            return
        lc = self.pair(lane)[1]
        idle = lc.clock() - lc.last_activity if lc is not None else time.time() - self._hold_at.get(lane, 0)
        if idle > self.hold_idle_s:
            self.release_control(lane)

    def _pause(self, jr: JobRun) -> None:
        lane = jr.lane
        if lane not in self.held_lanes or lane.startswith("phone:"):  # the owner drives the emulator and the adb phones; a real phone has no owner control
            return
        self._set_status(jr, "Paused: you have control of the phone")
        with self._cv:
            while lane in self.held_lanes and not jr.cancel.is_set():
                self._cv.wait(0.5)
                self._expire_hold(lane)
        self._set_status(jr, "Working…")

    # --- answers from chat ---------------------------------------------------------------------------------------
    def resume(self, job_id: str, value: dict) -> bool:
        jr = self.runs.get(job_id)
        if not jr or not jr.pending_item:
            return False
        jr.resume_value = value
        jr.resume.set()
        return True

    # --- internals -----------------------------------------------------------------------------------------------
    def _set_status(self, jr: JobRun, text: str) -> None:
        msg = self.db.update_message(jr.status_msg_id, content=text[:140])
        self.hub.publish(jr.bot_id, {"type": "message", "message": msg})

    def _drop_status(self, jr: JobRun) -> None:
        self.db.delete_message(jr.status_msg_id)
        self.hub.publish(jr.bot_id, {"type": "message_delete", "id": jr.status_msg_id})

    # --- scheduling ----------------------------------------------------------------------------------------------
    def _verdict(self, jr: JobRun) -> str:
        """run, wait or skip: a job with `after` runs once that job is done, waits while it is still active, and is skipped if it failed or was stopped."""
        dep = self.db.get_job(jr.after) if jr.after else None
        if dep is None or dep["status"] == "done":
            return "run"
        return "wait" if dep["status"] in ACTIVE else "skip"

    def _take_next(self, lane: str = "") -> JobRun:
        """Block until a queued job of this lane can run and claim it (the oldest runnable one, so a job waiting on a dependency never blocks the others)."""
        while True:
            skipped, pick = [], None
            with self._cv:
                for jr in [r for r in self._pending if r.lane == lane]:
                    v = self._verdict(jr)
                    if v == "skip":
                        skipped.append(jr)
                        self._pending.remove(jr)
                    elif v == "run" and pick is None and (lane != "" or not self.external_busy()):  # a task started from the web page uses the emulator
                        pick = jr
                if pick:
                    self._pending.remove(pick)
                    self._running[lane] = pick  # claimed under the lock, so busy() never reads "idle" between the queue and the phone
                elif not skipped:
                    self._cv.wait(1.0)  # woken by a new job, a finished job or a cancel; the timeout covers external changes
            self._expire_hold(lane)
            for jr in skipped:
                self._skip(jr)
            if pick:
                return pick

    def _skip(self, jr: JobRun) -> None:
        dep = self.db.get_job(jr.after) or {}
        why = f"skipped because the task before it did not finish ({dep.get('status', 'gone')}): {dep.get('goal', '')[:100]}"
        job = self.db.update_job(jr.job_id, status="skipped", ended=time.time(), result=why)
        self._drop_status(jr)
        self.runs.pop(jr.job_id, None)
        self.orch.job_finished(jr.bot_id, job)
        self._announce_queue()

    def _announce_queue(self) -> None:
        """Tell each queued job in its chat what it is waiting for."""
        with self._cv:
            pending, running = list(self._pending), dict(self._running)
        seen: dict[str, int] = {}
        for jr in pending:
            i, cur = seen.get(jr.lane, 0), running.get(jr.lane)
            seen[jr.lane] = i + 1
            dep = self.db.get_job(jr.after) if jr.after else None
            if dep and dep["status"] in ACTIVE:
                text = f"Waiting for an earlier task first: {dep['goal'][:60]}"
            elif cur:
                job = self.db.get_job(cur.job_id) or {}
                who = (self.db.get_bot(cur.bot_id) or {}).get("name", "another bot")
                text = f"Queued ({i + 1}): {who}'s task is " + ("waiting for you" if job.get("status") == "waiting" else "using the phone")
            else:
                text = f"Queued ({i + 1})"
            try:
                self._set_status(jr, text)
            except Exception:
                pass  # the status line is cosmetic

    def _loop(self, lane: str = "") -> None:
        while True:
            jr = self._take_next(lane)
            try:
                self._execute(jr)
            except Exception as e:  # never let the worker thread die
                self.db.update_job(jr.job_id, status="failed", ended=time.time(), result=f"{type(e).__name__}: {e}")
            finally:
                self._running.pop(lane, None)
                self.runs.pop(jr.job_id, None)
                with self._cv:
                    self._cv.notify_all()
                self._announce_queue()

    def _fail(self, jr: JobRun, result: str) -> None:
        self.db.update_job(jr.job_id, status="failed", ended=time.time(), result=result)
        self._drop_status(jr)
        self.orch.job_finished(jr.bot_id, self.db.get_job(jr.job_id))

    def _execute(self, jr: JobRun) -> None:
        self.db.update_job(jr.job_id, status="running")
        self._announce_queue()
        self._set_status(jr, "Starting…")
        if jr.after:  # a follow-up step starts from what the step before it found
            dep = self.db.get_job(jr.after)
            if dep and dep["result"]:
                jr.goal += f"\nResult of the previous step: {dep['result'][:400]}"
                self.db.update_job(jr.job_id, goal=jr.goal)
        device, lifecycle = self.device, self.lifecycle
        if jr.target.startswith("virtual:"):  # Cloud phone 2: its own device and power, never the emulator
            device, lifecycle = self.pair(jr.target)
            if device is None:
                return self._fail(jr, "Cloud phone 2 is not set up on this computer (see docs/virtual-android.md).")
            name = jr.target.split(":", 1)[1]
            if lifecycle is None and self.adb_phones is not None and name in self.adb_phones.config and not self.adb_phones.online(name, fresh=True):
                return self._fail(jr, f"The {name} phone is offline: adb cannot reach it ({self.adb_phones.serial(name)}). Connect it, then ask again.")
        elif jr.target.startswith("phone:"):  # the owner's own phone: no emulator to wake, but the app on it has to be connected
            pid = self.phones.pick(jr.target.split(":", 1)[1]) if self.phones else None
            if not pid:
                return self._fail(jr, "Your phone is not connected. Open the Mobile Agent app on it (and allow it to run in the background), then ask again.")
            device, lifecycle = RemoteDevice(self.phones, pid), None
        if lifecycle:
            if lifecycle.power != "on":
                self._set_status(jr, "Waking the phone…")  # a sleeping phone resumes in about a second, a stopped one boots in about a minute
            if not lifecycle.ensure_healthy(480):
                return self._fail(jr, "The phone did not wake up in time")
            self._set_status(jr, "Starting…")
        pushed: list[str] = []
        if jr.files:  # pictures the task needs go to the phone's Pictures/MobileAgent folder first, and are removed when it ends
            try:
                pushed = self._push_files(jr, device)
            except Exception as e:
                return self._fail(jr, f"I could not copy the picture to the phone: {str(e)[:150]}")
        if jr.report in ("link", "matrix") and (jr.target.startswith("phone:") or not hasattr(device, "set_night")):
            return self._fail(jr, "Link checks and screen-setting checks run only on the agent's own cloud phones, never on your own phone.")
        art = Artifacts()
        if jr.report in ("guide", "matrix", "plain"):
            cloudtools.snapshot(device, art)  # whatever the task changes in Settings is put back afterwards
        run_goal = jr.goal + REPORT_GOAL.get(jr.report, "")
        trace = self.data_dir / "runs" / jr.job_id
        status, result, attempt = "failed", "", 0
        while True:
            jr.notes.clear()  # updates sent while it was queued, or before a retry, are already part of the goal
            try:
                res = run_graph(run_goal, device, self.model_factory(WORKER_MODEL), self.max_steps + (15 if jr.report in ("guide", "matrix") else 0), trace_dir=trace,
                                settle=self.settle, handler=lambda p: self._handler(jr, p), emit=lambda e: self._emit(jr, e),
                                cancel=jr.cancel, pause=lambda: self._pause(jr), graph=self.graph, steer=lambda: self._take_notes(jr),
                                secrets=SecretUse(self.vault, jr.job_id) if self.vault else None, artifacts=art, observe_only=jr.report == "link")
                status, result = res.status, res.answer
                break
            except Exception as e:
                if lifecycle and attempt < self.phone_retries and is_phone_error(e) and not jr.cancel.is_set():
                    attempt += 1  # the phone hung or went offline mid-task: restart it and run the task once more
                    self._set_status(jr, "The phone stopped responding. Restarting it…")
                    if lifecycle.recover(300):
                        self._set_status(jr, "Starting again…")
                        continue
                result = f"{type(e).__name__}: {str(e)[:200]}"
                break
        self._remove_files(device, pushed)
        answer_text = result  # what the worker said, before the verdict is put in front of it for the job result
        undo_errors = art.finish()  # the display, dark mode and font size go back to what they were
        if jr.report == "link" and hasattr(device, "clear_app"):
            try:
                device.clear_app("com.android.chrome")  # the throwaway browser keeps nothing from the suspicious page
            except Exception as e:
                undo_errors.append(f"could not clear the browser: {e}"[:160])
        if art.verdict and status == "done":
            result = f"Verdict: {report_mod.VERDICT_LABEL[art.verdict['level']]}. " + "; ".join(art.verdict["reasons"][:4]) + f" | {result}"
        if status == "done" and jr.verify:
            ok, detail = run_check(device, jr.verify)
            if not ok:  # the worker said it was done but the phone disagrees: the job counts as failed, so tasks that depend on it do not run
                status, result = "failed", f"The worker reported done, but the check on the phone failed: {detail}. Worker said: {result}"[:500]
        if jr.timed_out:
            status, result = "failed", f"Timed out after {round(self.owner_wait_s / 60)} min waiting for the owner. The task was stopped and the phone released."
        job = self.db.update_job(jr.job_id, status=status, ended=time.time(), result=result, trace_dir=str(trace))
        if jr.target != "phone" and not jr.target.startswith("phone:") and hasattr(device, "third_party_packages"):  # remember what is installed while the phone is awake
            try:
                self.remember_apps("emulator" if not jr.target else jr.target.split(":", 1)[1], device.third_party_packages())
            except Exception:
                pass
        if lifecycle:
            lifecycle.touch()  # the idle timer starts when the task ends
        self._drop_status(jr)
        self._close_items(jr)
        self._save_artifact(jr, trace, status)
        self._save_report(jr, art, status, answer_text, undo_errors)
        self.orch.job_finished(jr.bot_id, job)

    PICTURE_DIR = "/sdcard/Pictures/MobileAgent"

    def _push_files(self, jr: JobRun, device) -> list[str]:
        """Copy the task's pictures to the phone and tell the worker where they are. Not on the owner's real phone yet (its app has no file hand-off)."""
        if not hasattr(device, "push"):
            raise ValueError("sending a picture to your own phone is not supported yet")
        remotes = []
        try:
            for fid in jr.files:
                row = self.db.get_attachment(fid)
                if not row or not Path(row["path"]).exists():
                    continue
                remote = f"{self.PICTURE_DIR}/{Path(row['path']).name}"
                device.push(row["path"], remote)
                remotes.append(remote)
                device.media_scan(remote)
        except Exception:
            self._remove_files(device, remotes)  # nothing half-copied is left behind
            raise
        if not remotes:
            raise ValueError("the picture is no longer available")
        names = ", ".join(r.rsplit("/", 1)[1] for r in remotes)
        jr.goal += (f"\nThe owner's picture(s) are already on this phone in Pictures/MobileAgent ({names}); they show in the gallery and in the photo picker of other apps. "
                    "Pick them from there; do not search the web for the picture.")
        self.db.update_job(jr.job_id, goal=jr.goal)
        return remotes

    @staticmethod
    def _remove_files(device, remotes: list[str]) -> None:
        for remote in remotes:
            try:
                device.remove_file(remote)
            except Exception:
                pass  # the phone may be gone; the copy is only a few hundred KB in a folder of its own

    @staticmethod
    def _take_notes(jr: JobRun) -> list[str]:
        out, jr.notes[:] = list(jr.notes), []
        return out

    def _close_items(self, jr: JobRun) -> None:
        """Cards left open when a job ends (for example after cancel) must not stay clickable."""
        for it in self.db.open_items(jr.bot_id):
            if it["job_id"] == jr.job_id:
                self.db.close_item(it["id"], "cancelled")
                m = self.db.get_message(it["message_id"]) if it["message_id"] else None
                if m:
                    upd = self.db.update_message(m["id"], meta={**m["meta"], "status": "cancelled"})
                    self.hub.publish(jr.bot_id, {"type": "message", "message": upd})

    def _emit(self, jr: JobRun, event: dict) -> None:
        if event["type"] == "step":
            self._set_status(jr, f"Step {event['n']}: {event['thought'] or event['action']['type']}")

    def _handler(self, jr: JobRun, payload: dict) -> dict:
        kind = payload["kind"]
        jr.resume.clear()
        if kind == "need_info":
            out = self.orch.worker_request(jr.bot_id, self.db.get_job(jr.job_id), payload)
            if "value" in out:
                return {"value": out["value"]}
            item = out["item"]
        else:
            text = payload.get("text", "")
            spec = {"type_secret": {"kind": "confirm", "question": text},
                    "need_secret": {"kind": "secret", "label": payload.get("label"), "secret_kind": payload.get("secret_kind"),
                                    "question": text or f"I need your {payload.get('label')} to continue. Enter it here to save it securely."},
                    "ask": {"kind": "confirm", "question": text},
                    "handoff": {"kind": "handoff", "question": text or "Please take over the phone for this step."},
                    "request_otp": {"kind": "otp", "question": f"{text or 'An app'} sent a one-time code. Enter it here."}}[kind]
            item = self.orch.make_item(jr.bot_id, jr.job_id, spec)
        jr.pending_item = item["id"]
        self.db.update_job(jr.job_id, status="waiting")
        self._set_status(jr, "Waiting for you")
        self._announce_queue()  # tasks queued behind this one now say they are waiting for the owner
        if not jr.resume.wait(self.owner_wait_s):
            self._expire(jr, item)
        jr.pending_item = None
        if not jr.cancel.is_set():
            self.db.update_job(jr.job_id, status="running")
            self._set_status(jr, "Working…")
            self._announce_queue()
        return jr.resume_value

    def _expire(self, jr: JobRun, item: dict) -> None:
        """The owner did not answer in time: close the card, stop the task and give the phone to the next one."""
        jr.timed_out = True
        jr.resume_value = {"cancelled": True}
        jr.cancel.set()
        self.db.close_item(item["id"], "expired")
        m = self.db.get_message(item["message_id"]) if item.get("message_id") else None
        if m:
            self.hub.publish(jr.bot_id, {"type": "message", "message": self.db.update_message(m["id"], meta={**m["meta"], "status": "expired"})})

    def _save_report(self, jr: JobRun, art: Artifacts, status: str, result: str, undo_errors: list[str]) -> None:
        """A task that captured pictures or recorded a verdict leaves a report in the Library and a card in the chat."""
        if not (art.captures or art.verdict):
            return
        kind = jr.report or "plain"
        label = {"": "Cloud phone", "virtual:games": "Cloud phone 2"}.get(jr.target, jr.target.split(":", 1)[-1] if jr.target else "")
        title = report_mod.title_for(kind, jr.goal)
        reportsave.save(self.db, self.hub, self.data_dir, jr.bot_id, jr.job_id, kind, title, jr.goal.splitlines()[0], status, result, art, label,
                        [f"Could not put the phone back fully: {e}" for e in undo_errors])

    def _save_artifact(self, jr: JobRun, trace: Path, status: str) -> None:
        shots = sorted(trace.glob("step*.png")) if trace.exists() else []
        if not shots:
            return
        dest = self.data_dir / "library" / f"{jr.job_id}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(shots[-1], dest)
        title = jr.goal[:80] + ("" if status == "done" else f" ({status})")
        self.db.add_library(jr.bot_id, jr.job_id, title, str(dest))
