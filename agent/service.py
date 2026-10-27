"""FastAPI service: submit goals, stream steps, answer approvals / OTP requests, and take over the phone."""
import asyncio
import hmac
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field

from agent.graph import Result, build_graph, run as run_graph

MODELS = {"muse-wonder-1.0", "muse-spark-1.3", "muse-spark-1.2", "muse-spark-1.1"}
TERMINAL = {"done", "failed", "denied", "cancelled", "max_steps", "error"}
WEB = Path(__file__).resolve().parent.parent / "web"


class Run:
    def __init__(self, goal: str, model: str):
        self.id, self.goal, self.model = uuid.uuid4().hex[:12], goal, model
        self.status, self.answer, self.pending = "running", "", None
        self.events: list[dict] = []
        self.cancel = threading.Event()
        self._resume = threading.Event()
        self._resume_value: dict = {}
        self._lock = threading.Lock()
        self.emit({"type": "status", "status": "running"})

    def emit(self, event: dict) -> None:
        with self._lock:
            self.events.append({**event, "seq": len(self.events), "ts": round(time.time(), 2)})

    def wait_for_owner(self, payload: dict) -> dict:
        self.pending, self.status = payload, "waiting"
        self._resume.clear()
        self.emit({"type": "waiting", **payload})
        self._resume.wait()
        self.pending, self.status = None, "running"
        self.emit({"type": "status", "status": "running"})
        return self._resume_value

    def resume(self, value: dict) -> None:
        self._resume_value = value
        self._resume.set()

    def stop(self) -> None:
        self.cancel.set()
        self.resume({"cancelled": True})

    def finish(self, res: Result) -> None:
        self.status, self.answer = res.status, res.answer
        self.emit({"type": "final", "status": res.status, "answer": res.answer, "steps": res.steps})

    def summary(self) -> dict:
        return {"id": self.id, "goal": self.goal, "model": self.model, "status": self.status,
                "answer": self.answer, "pending": self.pending, "events": len(self.events)}


class NewRun(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    model: str = "muse-wonder-1.0"
    max_steps: int = Field(default=20, ge=1, le=60)


class Resume(BaseModel):
    approved: bool | None = None
    done: bool | None = None
    code: str | None = Field(default=None, max_length=12)


class Tap(BaseModel):
    fx: float = Field(ge=0, le=1)
    fy: float = Field(ge=0, le=1)


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class Key(BaseModel):
    key: str = Field(pattern="^(back|home|enter|recents)$")


class Swipe(BaseModel):
    direction: str = Field(pattern="^(up|down|left|right)$")


def create_app(device, model_factory, token: str | None = None, settle: float = 0.6) -> FastAPI:
    app = FastAPI(title="Mobile Agent")
    runs: dict[str, Run] = {}
    graph = build_graph()
    state = {"active": None}
    guard = threading.Lock()

    def auth(authorization: str | None = Header(default=None), token_q: str | None = Query(default=None, alias="token")):
        if not token:
            return
        given = (authorization or "").removeprefix("Bearer ").strip() or (token_q or "")
        if not hmac.compare_digest(given, token):
            raise HTTPException(401, "invalid token")

    def active() -> Run | None:
        r = state["active"]
        return r if r and r.status not in TERMINAL else None

    def get_run(run_id: str) -> Run:
        if run_id not in runs:
            raise HTTPException(404, "unknown run")
        return runs[run_id]

    def takeover_allowed() -> None:
        r = active()
        if r and r.status != "waiting":
            raise HTTPException(409, "the agent is acting on the phone; wait for it to pause or cancel the run")

    def worker(r: Run, max_steps: int) -> None:
        try:
            model = model_factory(r.model)
            res = run_graph(r.goal, device, model, max_steps, trace_dir=f"runs/{time.strftime('%Y%m%d-%H%M%S')}-{r.id}",
                            settle=settle, handler=r.wait_for_owner, emit=r.emit, cancel=r.cancel, graph=graph)
            r.finish(res)
        except Exception as e:
            r.finish(Result("error", f"{type(e).__name__}: {str(e)[:200]}"))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "auth": bool(token), "models": sorted(MODELS)}

    @app.post("/api/runs", dependencies=[Depends(auth)])
    def create_run(body: NewRun):
        if body.model not in MODELS:
            raise HTTPException(400, f"model must be one of {sorted(MODELS)}")
        with guard:
            if active():
                raise HTTPException(409, "a run is already active on this phone")
            r = Run(body.goal.strip(), body.model)
            runs[r.id] = r
            state["active"] = r
            for old in list(runs)[:-20]:
                runs.pop(old, None)
        threading.Thread(target=worker, args=(r, body.max_steps), daemon=True).start()
        return r.summary()

    @app.get("/api/runs", dependencies=[Depends(auth)])
    def list_runs():
        return [r.summary() for r in reversed(list(runs.values()))]

    @app.get("/api/runs/{run_id}", dependencies=[Depends(auth)])
    def read_run(run_id: str):
        r = get_run(run_id)
        return {**r.summary(), "log": r.events}

    @app.get("/api/runs/{run_id}/events", dependencies=[Depends(auth)])
    async def stream(run_id: str, after: int = -1):
        r = get_run(run_id)

        async def gen():
            sent, idle = after + 1, 0
            while True:
                while sent < len(r.events):
                    yield f"id: {sent}\ndata: {json.dumps(r.events[sent])}\n\n"
                    sent, idle = sent + 1, 0
                if r.status in TERMINAL and sent >= len(r.events):
                    return
                idle += 1
                if idle % 60 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/runs/{run_id}/resume", dependencies=[Depends(auth)])
    def resume_run(run_id: str, body: Resume):
        r = get_run(run_id)
        p = r.pending
        if not p:
            raise HTTPException(409, "run is not waiting for the owner")
        kind = p["kind"]
        if kind == "ask":
            if body.approved is None:
                raise HTTPException(422, "approved is required")
            r.resume({"approved": body.approved})
        elif kind == "handoff":
            r.resume({"done": True})
        else:  # request_otp: the code goes straight to the device and is never logged or echoed
            if body.approved is False:
                r.resume({"code": ""})
            elif body.code and re.fullmatch(r"\d{4,8}", body.code):
                r.resume({"code": body.code})
            else:
                raise HTTPException(422, "code must be 4-8 digits")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(auth)])
    def cancel_run(run_id: str):
        get_run(run_id).stop()
        return {"ok": True}

    frame = {"at": 0.0, "jpeg": b""}
    frame_lock = threading.Lock()

    @app.get("/api/device/screenshot", dependencies=[Depends(auth)])
    def screenshot():
        # One capture at a time and a short cache, so live viewers never compete with the agent for ADB.
        with frame_lock:
            if time.time() - frame["at"] > (2.5 if active() and not state["active"].pending else 0.8):
                img = Image.open(io.BytesIO(device.screenshot())).convert("RGB")
                img.thumbnail((540, 1200))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=70)
                frame.update(at=time.time(), jpeg=buf.getvalue())
            jpeg = frame["jpeg"]
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/device/tap", dependencies=[Depends(auth)])
    def tap(body: Tap):
        takeover_allowed()
        w, h = device.size()
        device.tap(round(body.fx * w), round(body.fy * h))
        return {"ok": True}

    @app.post("/api/device/type", dependencies=[Depends(auth)])
    def type_text(body: Text):
        takeover_allowed()
        device.type_text(body.text)
        return {"ok": True}

    @app.post("/api/device/key", dependencies=[Depends(auth)])
    def key(body: Key):
        takeover_allowed()
        device.key({"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER",
                    "recents": "KEYCODE_APP_SWITCH"}[body.key])
        return {"ok": True}

    @app.post("/api/device/swipe", dependencies=[Depends(auth)])
    def swipe(body: Swipe):
        takeover_allowed()
        w, h = device.size()
        cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
        d = {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
             "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}[body.direction]
        device.swipe(*d)
        return {"ok": True}

    return app


def create_default_app() -> FastAPI:
    from agent.device import Device
    from agent.model import MetaClient, load_env
    load_env()
    return create_app(Device(os.environ.get("ANDROID_SERIAL")), lambda m: MetaClient(m),
                      token=os.environ.get("APP_TOKEN") or None)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    load = create_default_app()
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("APP_TOKEN"):
        raise SystemExit("Refusing to listen on a public address without APP_TOKEN set")
    uvicorn.run(load, host=host, port=int(os.environ.get("PORT", "8000")))
