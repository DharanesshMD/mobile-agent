"""Background phone jobs: one shared phone, FIFO queue, owner takeover, and worker gates turned into chat cards."""
import queue
import shutil
import threading
import time
from pathlib import Path

from agent.config import WORKER_MODEL
from agent.db import Database
from agent.graph import build_graph, run as run_graph
from agent.hub import Hub

ACTIVE = ("queued", "running", "waiting")


class JobRun:
    def __init__(self, job: dict, status_msg_id: int):
        self.job_id, self.bot_id, self.goal = job["id"], job["bot_id"], job["goal"]
        self.status_msg_id = status_msg_id
        self.cancel = threading.Event()
        self.resume = threading.Event()
        self.resume_value: dict = {}
        self.pending_item: str | None = None


class JobManager:
    def __init__(self, db: Database, hub: Hub, device, model_factory, data_dir: str | Path = "data",
                 settle: float = 0.6, max_steps: int = 25, external_busy=None):
        self.db, self.hub, self.device, self.model_factory = db, hub, device, model_factory
        self.data_dir, self.settle, self.max_steps = Path(data_dir), settle, max_steps
        self.external_busy = external_busy or (lambda: False)
        self.orch = None  # set by the app after construction
        self.graph = build_graph()
        self.runs: dict[str, JobRun] = {}
        self.current: JobRun | None = None
        self._q: queue.Queue = queue.Queue()
        self._cv = threading.Condition()
        self.held = False
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="phone-jobs")
        self._thread.start()

    def recover(self) -> None:
        """After a restart nothing is really running: fail leftover jobs and close their cards."""
        for j in self.db._q("select id from jobs where status in ('queued','running','waiting')"):
            self.db.update_job(j["id"], status="failed", ended=time.time(), result="interrupted by a restart")
        for it in self.db.open_items():
            if it["job_id"]:
                self.db.close_item(it["id"], "cancelled")
                if it["message_id"]:
                    m = self.db.get_message(it["message_id"])
                    if m:
                        self.db.update_message(it["message_id"], meta={**m["meta"], "status": "cancelled"})
        for m in self.db._q("select id from messages where kind='status'"):
            self.db.delete_message(m["id"])

    # --- queries -------------------------------------------------------------------------------------------------
    def active_for(self, bot_id: str) -> dict | None:
        rows = self.db._q(f"select * from jobs where bot_id=? and status in ({','.join('?' * len(ACTIVE))}) order by started limit 1",
                          (bot_id, *ACTIVE))
        return rows[0] if rows else None

    def busy(self) -> bool:
        return self.current is not None or not self._q.empty()

    def state(self) -> dict:
        cur = self.current
        job = self.db.get_job(cur.job_id) if cur else None
        if self.held:
            state = "control"
        elif job:
            state = "waiting" if job["status"] == "waiting" else "working"
        else:
            state = "ready"
        return {"state": state, "held": self.held, "job": job, "queued": self._q.qsize()}

    # --- submit and cancel ---------------------------------------------------------------------------------------
    def submit(self, bot_id: str, goal: str) -> dict:
        job = self.db.create_job(bot_id, goal)
        text = "Waiting for the phone…" if self.busy() else "Starting…"
        msg = self.db.add_message(bot_id, "assistant", "status", text, {"job_id": job["id"]})
        self.hub.publish(bot_id, {"type": "message", "message": msg})
        jr = JobRun(job, msg["id"])
        self.runs[job["id"]] = jr
        self._q.put(jr)
        return job

    def cancel(self, job_id: str) -> bool:
        jr = self.runs.get(job_id)
        if not jr:
            return False
        jr.cancel.set()
        jr.resume_value = {"cancelled": True}
        jr.resume.set()
        with self._cv:
            self._cv.notify_all()
        job = self.db.get_job(job_id)
        if job and job["status"] in ("running", "waiting"):
            self._set_status(jr, "Stopping after the current step…")
        if job and job["status"] == "queued":  # never started: close it here
            self.db.update_job(job_id, status="cancelled", ended=time.time())
            self._drop_status(jr)
            self.orch.job_finished(jr.bot_id, self.db.get_job(job_id))
        return True

    # --- owner control of the phone ------------------------------------------------------------------------------
    def take_control(self) -> None:
        with self._cv:
            self.held = True

    def release_control(self) -> None:
        with self._cv:
            self.held = False
            self._cv.notify_all()

    def _pause(self, jr: JobRun) -> None:
        if not self.held:
            return
        self._set_status(jr, "Paused: you have control of the phone")
        with self._cv:
            while self.held and not jr.cancel.is_set():
                self._cv.wait(0.5)
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

    def _loop(self) -> None:
        while True:
            jr = self._q.get()
            if jr is None:
                return
            job = self.db.get_job(jr.job_id)
            if not job or job["status"] == "cancelled":
                continue
            while self.external_busy():
                time.sleep(0.5)
            self.current = jr
            try:
                self._execute(jr)
            except Exception as e:  # never let the worker thread die
                self.db.update_job(jr.job_id, status="failed", ended=time.time(), result=f"{type(e).__name__}: {e}")
            finally:
                self.current = None
                self.runs.pop(jr.job_id, None)

    def _execute(self, jr: JobRun) -> None:
        self.db.update_job(jr.job_id, status="running")
        self._set_status(jr, "Starting…")
        trace = self.data_dir / "runs" / jr.job_id
        status, result = "failed", ""
        try:
            res = run_graph(jr.goal, self.device, self.model_factory(WORKER_MODEL), self.max_steps, trace_dir=trace,
                            settle=self.settle, handler=lambda p: self._handler(jr, p), emit=lambda e: self._emit(jr, e),
                            cancel=jr.cancel, pause=lambda: self._pause(jr), graph=self.graph)
            status, result = res.status, res.answer
        except Exception as e:
            result = f"{type(e).__name__}: {str(e)[:200]}"
        job = self.db.update_job(jr.job_id, status=status, ended=time.time(), result=result, trace_dir=str(trace))
        self._drop_status(jr)
        self._close_items(jr)
        self._save_artifact(jr, trace, status)
        self.orch.job_finished(jr.bot_id, job)

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
            spec = {"ask": {"kind": "confirm", "question": text},
                    "handoff": {"kind": "handoff", "question": text or "Please take over the phone for this step."},
                    "request_otp": {"kind": "otp", "question": f"{text or 'An app'} sent a one-time code. Enter it here."}}[kind]
            item = self.orch.make_item(jr.bot_id, jr.job_id, spec)
        jr.pending_item = item["id"]
        self.db.update_job(jr.job_id, status="waiting")
        self._set_status(jr, "Waiting for you")
        jr.resume.wait()
        jr.pending_item = None
        if not jr.cancel.is_set():
            self.db.update_job(jr.job_id, status="running")
            self._set_status(jr, "Working…")
        return jr.resume_value

    def _save_artifact(self, jr: JobRun, trace: Path, status: str) -> None:
        shots = sorted(trace.glob("step*.png")) if trace.exists() else []
        if not shots:
            return
        dest = self.data_dir / "library" / f"{jr.job_id}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(shots[-1], dest)
        title = jr.goal[:80] + ("" if status == "done" else f" ({status})")
        self.db.add_library(jr.bot_id, jr.job_id, title, str(dest))
