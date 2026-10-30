"""Background phone jobs: one shared phone, a queue that respects dependencies, owner takeover, and worker gates turned into chat cards.

Jobs run one at a time in the order they were asked, except that a job with `after` waits until that job is done and is skipped if it did not
succeed. A job waiting for the owner gives the phone up after OWNER_WAIT_MIN, owner control of the phone ends after HOLD_IDLE_MIN without input,
and a job that hits a dead phone restarts it and runs once more, so the queue cannot freeze on one stuck task or one dead emulator.
"""
import shutil
import subprocess
import threading
import time
from pathlib import Path

from agent.checks import run_check
from agent.config import HOLD_IDLE_MIN, OWNER_WAIT_MIN, WORKER_MODEL
from agent.db import Database
from agent.graph import build_graph, run as run_graph
from agent.hub import Hub
from agent.vault import SecretUse, Vault

ACTIVE = ("queued", "running", "waiting")
PHONE_ERROR_WORDS = ("adb", "device offline", "device not found", "no devices", "uiautomator", "connection refused", "timed out")


def is_phone_error(e: Exception) -> bool:
    """Did the phone itself fail (hung, offline, adb died), as opposed to a bug or a bad action? Those are worth a restart and one more try."""
    if isinstance(e, (subprocess.TimeoutExpired, subprocess.CalledProcessError, ConnectionError, TimeoutError, OSError)):
        return True
    return not isinstance(e, (ValueError, KeyError, TypeError)) and any(w in str(e).lower() for w in PHONE_ERROR_WORDS)


class JobRun:
    def __init__(self, job: dict, status_msg_id: int):
        self.job_id, self.bot_id, self.goal, self.after, self.verify = job["id"], job["bot_id"], job["goal"], job.get("after"), job.get("verify")
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
        self.orch = None  # set by the app after construction
        self.graph = build_graph()
        self.runs: dict[str, JobRun] = {}
        self.current: JobRun | None = None
        self._pending: list[JobRun] = []  # queued jobs in the order they were asked
        self._cv = threading.Condition()
        self.held = False
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="phone-jobs")
        self._thread.start()

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

    def busy(self) -> bool:
        return self.current is not None or bool(self._pending)

    def state(self) -> dict:
        cur = self.current
        job = self.db.get_job(cur.job_id) if cur else None
        if self.held:
            state = "control"
        elif job:
            state = "waiting" if job["status"] == "waiting" else "working"
        else:
            state = "ready"
        power = self.lifecycle.info() if self.lifecycle else {"power": "on"}
        try:  # never ask a sleeping phone anything: its adb connection would hang
            orientation = self.device.orientation() if power["power"] == "on" and hasattr(self.device, "orientation") else "portrait"
        except Exception:
            orientation = "portrait"
        return {"state": state, "held": self.held, "job": job, "queued": len(self._pending), "queue": [{"id": r.job_id, "bot_id": r.bot_id, "goal": r.goal[:100], "after": r.after} for r in self._pending], "orientation": orientation, "phone": power}

    # --- submit and cancel ---------------------------------------------------------------------------------------
    def submit(self, bot_id: str, goal: str, after: str | None = None, verify: dict | None = None, routine: str | None = None) -> dict:
        """Queue a phone job. With `after` (a job id) it waits for that job to finish successfully and is skipped if it does not.
        With `verify` (a phone check) the worker's "done" only counts if the phone really shows it."""
        job = self.db.create_job(bot_id, goal, after, verify, routine)
        msg = self.db.add_message(bot_id, "assistant", "status", "Waiting for the phone…" if self.busy() or after else "Starting…", {"job_id": job["id"]})
        self.hub.publish(bot_id, {"type": "message", "message": msg})
        jr = JobRun(job, msg["id"])
        self.runs[job["id"]] = jr
        with self._cv:
            self._pending.append(jr)
            self._cv.notify_all()
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
    def take_control(self) -> None:
        if self.lifecycle:
            self.lifecycle.wake()
        with self._cv:
            self.held = True

    def release_control(self) -> None:
        with self._cv:
            self.held = False
            self._cv.notify_all()

    def _expire_hold(self) -> None:
        """Owner control that is forgotten (no input for HOLD_IDLE_MIN) ends by itself, or every queued task would wait for ever."""
        lc = self.lifecycle
        if self.held and lc is not None and lc.clock() - lc.last_activity > self.hold_idle_s:
            self.release_control()

    def _pause(self, jr: JobRun) -> None:
        if not self.held:
            return
        self._set_status(jr, "Paused: you have control of the phone")
        with self._cv:
            while self.held and not jr.cancel.is_set():
                self._cv.wait(0.5)
                self._expire_hold()
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

    def _take_next(self) -> JobRun:
        """Block until a queued job can run and claim it (the oldest runnable one, so a job waiting on a dependency never blocks the others)."""
        while True:
            skipped, pick = [], None
            with self._cv:
                for jr in list(self._pending):
                    v = self._verdict(jr)
                    if v == "skip":
                        skipped.append(jr)
                        self._pending.remove(jr)
                    elif v == "run" and pick is None and not self.external_busy():
                        pick = jr
                if pick:
                    self._pending.remove(pick)
                    self.current = pick  # claimed under the lock, so busy() never reads "idle" between the queue and the phone
                elif not skipped:
                    self._cv.wait(1.0)  # woken by a new job, a finished job or a cancel; the timeout covers external changes
            self._expire_hold()
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
            pending, cur = list(self._pending), self.current
        for i, jr in enumerate(pending):
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

    def _loop(self) -> None:
        while True:
            jr = self._take_next()
            try:
                self._execute(jr)
            except Exception as e:  # never let the worker thread die
                self.db.update_job(jr.job_id, status="failed", ended=time.time(), result=f"{type(e).__name__}: {e}")
            finally:
                self.current = None
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
        if self.lifecycle:
            if self.lifecycle.power != "on":
                self._set_status(jr, "Waking the phone…")  # a sleeping phone resumes in about a second, a stopped one boots in about a minute
            if not self.lifecycle.ensure_healthy(300):
                return self._fail(jr, "The phone did not wake up in time")
            self._set_status(jr, "Starting…")
        trace = self.data_dir / "runs" / jr.job_id
        status, result, attempt = "failed", "", 0
        while True:
            jr.notes.clear()  # updates sent while it was queued, or before a retry, are already part of the goal
            try:
                res = run_graph(jr.goal, self.device, self.model_factory(WORKER_MODEL), self.max_steps, trace_dir=trace,
                                settle=self.settle, handler=lambda p: self._handler(jr, p), emit=lambda e: self._emit(jr, e),
                                cancel=jr.cancel, pause=lambda: self._pause(jr), graph=self.graph, steer=lambda: self._take_notes(jr),
                                secrets=SecretUse(self.vault, jr.job_id) if self.vault else None)
                status, result = res.status, res.answer
                break
            except Exception as e:
                if self.lifecycle and attempt < self.phone_retries and is_phone_error(e) and not jr.cancel.is_set():
                    attempt += 1  # the phone hung or went offline mid-task: restart it and run the task once more
                    self._set_status(jr, "The phone stopped responding. Restarting it…")
                    if self.lifecycle.recover(300):
                        self._set_status(jr, "Starting again…")
                        continue
                result = f"{type(e).__name__}: {str(e)[:200]}"
                break
        if status == "done" and jr.verify:
            ok, detail = run_check(self.device, jr.verify)
            if not ok:  # the worker said it was done but the phone disagrees: the job counts as failed, so tasks that depend on it do not run
                status, result = "failed", f"The worker reported done, but the check on the phone failed: {detail}. Worker said: {result}"[:500]
        if jr.timed_out:
            status, result = "failed", f"Timed out after {round(self.owner_wait_s / 60)} min waiting for the owner. The task was stopped and the phone released."
        job = self.db.update_job(jr.job_id, status=status, ended=time.time(), result=result, trace_dir=str(trace))
        if self.lifecycle:
            self.lifecycle.touch()  # the idle timer starts when the task ends
        self._drop_status(jr)
        self._close_items(jr)
        self._save_artifact(jr, trace, status)
        self.orch.job_finished(jr.bot_id, job)

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

    def _save_artifact(self, jr: JobRun, trace: Path, status: str) -> None:
        shots = sorted(trace.glob("step*.png")) if trace.exists() else []
        if not shots:
            return
        dest = self.data_dir / "library" / f"{jr.job_id}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(shots[-1], dest)
        title = jr.goal[:80] + ("" if status == "done" else f" ({status})")
        self.db.add_library(jr.bot_id, jr.job_id, title, str(dest))
