"""FastAPI service: submit goals, stream steps, answer approvals / OTP requests, and take over the phone."""
import asyncio
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field

from agent.graph import Result, build_graph, run as run_graph
from agent.chat_api import register_chat
from agent.vault import Vault, load_key
from agent.db import Database
from agent.hub import Hub
from agent.device import is_blank
from agent.remote import PhoneHub
from agent.jobs import JobManager
from agent.orchestrator import Orchestrator
from agent.master import Watcher, ensure_games_bot, ensure_master, ensure_phone_bot
from agent.routines import Scheduler
from agent.store import Store

MODELS = {"muse-wonder-1.0", "muse-spark-1.3", "muse-spark-1.2", "muse-spark-1.1"}
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
PAIR_TTL = 600
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
TERMINAL = {"done", "failed", "denied", "cancelled", "max_steps", "error"}
WEB = Path(__file__).resolve().parent.parent / "web"


class Run:
    def __init__(self, goal: str, model: str, kind: str = "task"):
        self.id, self.goal, self.model, self.kind = uuid.uuid4().hex[:12], goal, model, kind
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
        return {"id": self.id, "goal": self.goal, "model": self.model, "kind": self.kind, "status": self.status,
                "answer": self.answer, "pending": self.pending, "events": len(self.events)}


class NewRun(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    model: str = "muse-wonder-1.0"
    max_steps: int = Field(default=20, ge=1, le=60)


class Resume(BaseModel):
    approved: bool | None = None
    done: bool | None = None
    code: str | None = Field(default=None, max_length=12)


class Pair(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    name: str = Field(default="phone", min_length=1, max_length=60)


class CloudAccountStart(BaseModel):
    consent: bool
    email: str | None = Field(default=None, max_length=320)


class Tap(BaseModel):
    fx: float = Field(ge=0, le=1)
    fy: float = Field(ge=0, le=1)


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=500)


KEYCODES = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER", "recents": "KEYCODE_APP_SWITCH",
            "delete": "KEYCODE_DEL", "forward_delete": "KEYCODE_FORWARD_DEL", "tab": "KEYCODE_TAB", "space": "KEYCODE_SPACE",
            "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
            "page_up": "KEYCODE_PAGE_UP", "page_down": "KEYCODE_PAGE_DOWN"}


class Rotate(BaseModel):
    orientation: str = Field(pattern="^(portrait|landscape|auto)$")


class Key(BaseModel):
    key: str = Field(pattern="^(" + "|".join(KEYCODES) + ")$")


class Swipe(BaseModel):
    """Either a direction (the scroll buttons) or a drag from one point to another in screen fractions (mouse drag, wheel, long press)."""
    direction: str | None = Field(default=None, pattern="^(up|down|left|right)$")
    fx1: float | None = Field(default=None, ge=0, le=1)
    fy1: float | None = Field(default=None, ge=0, le=1)
    fx2: float | None = Field(default=None, ge=0, le=1)
    fy2: float | None = Field(default=None, ge=0, le=1)
    ms: int = Field(default=300, ge=50, le=2000)


def create_app(device, model_factory, token: str | None = None, settle: float = 0.4, store: Store | None = None,
               db: Database | None = None, data_dir: str = "data", vault_key: bytes | None = None,
               start_scheduler: bool = False, master: bool = False, lifecycle=None, virtuals: dict | None = None) -> FastAPI:
    """`virtuals` maps a name ("games") to the (Device, Lifecycle) of a virtual phone, each with its own bot, lane and live view."""
    app = FastAPI(title="Mobile Agent")
    store = store or Store(None)
    runs: dict[str, Run] = {}
    codes: dict[str, float] = {}
    pair_fails = {"n": 0, "until": 0.0}
    graph = build_graph()
    state = {"active": None}
    guard = threading.Lock()

    def auth(authorization: str | None = Header(default=None), token_q: str | None = Query(default=None, alias="token")) -> dict:
        if not token:
            return {"kind": "open", "name": "local"}
        given = (authorization or "").removeprefix("Bearer ").strip() or (token_q or "")
        if given and hmac.compare_digest(given.encode(), token.encode()):
            return {"kind": "admin", "name": "admin"}
        dev = store.find_device(given) if given else None
        if dev:
            return {"kind": "device", **dev}
        raise HTTPException(401, "invalid token")

    def active() -> Run | None:
        r = state["active"]
        return r if r and r.status not in TERMINAL else None

    def get_run(run_id: str) -> Run:
        if run_id not in runs:
            raise HTTPException(404, "unknown run")
        return runs[run_id]

    def phone_on() -> None:
        """Owner actions and live frames need an awake phone; a sleeping one would hang adb."""
        if lifecycle:
            if lifecycle.power != "on":
                raise HTTPException(409, f"The phone is {'asleep' if lifecycle.power == 'sleeping' else 'off' if lifecycle.power == 'off' else 'waking up'}. Wake it first.")
            lifecycle.touch()

    def takeover_allowed() -> None:
        phone_on()
        r = active()
        if r and r.status != "waiting":
            raise HTTPException(409, "the agent is acting on the phone; wait for it to pause or cancel the run")
        cur = jobs.current
        if cur and not jobs.held and (jobs.db.get_job(cur.job_id) or {}).get("status") != "waiting":
            raise HTTPException(409, "the agent is acting on the phone; take control first or wait for it to pause")

    def google_accounts() -> list[str]:
        fn = getattr(device, "accounts", None)
        return sorted({n for n, t in (fn() if fn else []) if t == "com.google"})

    def sync_account(r: Run | None = None) -> str | None:
        """The device is the source of truth: record whichever Google account is really on it."""
        found = google_accounts()
        store.set_cloud_account(found[0] if found else None)
        if r:
            r.emit({"type": "account", "email": found[0] if found else None, "action": r.kind})
        return found[0] if found else None

    def start_run(goal: str, model: str, max_steps: int, kind: str = "task") -> Run:
        with guard:
            if active() or jobs.busy(""):
                raise HTTPException(409, "a run is already active on this phone")
            r = Run(goal, model, kind)
            runs[r.id] = r
            state["active"] = r
            for old in list(runs)[:-20]:
                runs.pop(old, None)
        threading.Thread(target=worker, args=(r, max_steps), daemon=True).start()
        return r

    def worker(r: Run, max_steps: int) -> None:
        try:
            model = model_factory(r.model)
            res = run_graph(r.goal, device, model, max_steps, trace_dir=f"runs/{time.strftime('%Y%m%d-%H%M%S')}-{r.id}",
                            settle=settle, handler=r.wait_for_owner, emit=r.emit, cancel=r.cancel, graph=graph)
            if r.kind in ("provision", "deprovision"):
                try:
                    sync_account(r)  # verify against the device, not the model's claim
                except Exception:
                    pass
            r.finish(res)
        except Exception as e:
            r.finish(Result("error", f"{type(e).__name__}: {str(e)[:200]}"))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})  # always pick up the newest page after an update

    @app.get("/api/health")
    def health():
        return {"ok": True, "auth": bool(token), "models": sorted(MODELS)}

    @app.post("/api/runs", dependencies=[Depends(auth)])
    def create_run(body: NewRun):
        if body.model not in MODELS:
            raise HTTPException(400, f"model must be one of {sorted(MODELS)}")
        return start_run(body.goal.strip(), body.model, body.max_steps).summary()

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

    def issue_code() -> str:
        now = time.time()
        for c in [c for c, exp in codes.items() if exp < now]:
            codes.pop(c)
        if len(codes) >= 5:
            codes.pop(next(iter(codes)))
        raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
        codes[raw] = now + PAIR_TTL
        return f"{raw[:4]}-{raw[4:]}"

    app.state.issue_pair_code = issue_code

    @app.post("/api/pair/code")
    def pair_code(who: dict = Depends(auth)):
        return {"code": issue_code(), "expires_in": PAIR_TTL}

    @app.post("/api/pair")
    def pair(body: Pair):
        now = time.time()
        if now < pair_fails["until"]:
            raise HTTPException(429, "too many wrong codes; wait a minute")
        given = re.sub(r"[^A-Z0-9]", "", body.code.upper())
        match = next((c for c, exp in codes.items() if exp > now and hmac.compare_digest(c.encode(), given.encode())), None)
        if not match:
            pair_fails["n"] += 1
            if pair_fails["n"] >= 5:
                pair_fails.update(n=0, until=now + 60)
            raise HTTPException(401, "invalid or expired pairing code")
        codes.pop(match)
        pair_fails["n"] = 0
        did, tok = store.add_device(body.name.strip())
        return {"device_id": did, "token": tok}

    @app.get("/api/devices")
    def list_devices(who: dict = Depends(auth)):
        return store.devices()

    @app.delete("/api/devices/{device_id}")
    def revoke_device(device_id: str, who: dict = Depends(auth)):
        if not store.revoke(device_id):
            raise HTTPException(404, "unknown device")
        return {"ok": True}

    @app.get("/api/me")
    def me(who: dict = Depends(auth)):
        acct = store.cloud_account()
        return {"principal": who, "google": acct["email"] if acct else None,
                "linked_at": acct["linked_at"] if acct else None, "devices": len(store.devices())}

    @app.get("/api/cloud-account")
    def cloud_account(who: dict = Depends(auth)):
        email = sync_account()
        acct = store.cloud_account()
        return {"linked": email, "linked_at": acct["linked_at"] if acct else None, "consent": store.consent(),
                "busy": bool(active())}

    @app.post("/api/cloud-account/start")
    def cloud_account_start(body: CloudAccountStart, who: dict = Depends(auth)):
        if not body.consent:
            raise HTTPException(422, "consent is required to set up a Google account on the cloud phone")
        email = (body.email or "").strip() or None
        if email and not EMAIL.match(email):
            raise HTTPException(422, "invalid email")
        if sync_account():
            raise HTTPException(409, "a Google account is already signed in on the cloud phone")
        goal = ("Set up a Google account on this phone. Open the Play Store app and tap Sign in "
                "(or Settings > Passwords & accounts > Add account > Google). "
                + (f"When the email field appears, type the email {email} and continue. " if email else "")
                + "When a password, verification code or any security check appears, use handoff so the owner completes it; "
                  "never type a password or code yourself. Once the Google account is signed in and visible "
                  "(for example the Play Store home screen), finish with done.")
        store.record_consent(email)
        return start_run(goal, "muse-wonder-1.0", 25, "provision").summary()

    @app.delete("/api/cloud-account")
    def cloud_account_remove(confirm: bool = False, who: dict = Depends(auth)):
        if not confirm:
            raise HTTPException(422, "pass confirm=true to remove the Google account from the cloud phone")
        email = sync_account()
        if not email:
            raise HTTPException(404, "no Google account on the cloud phone")
        goal = (f"Remove the Google account {email} from this phone: open Settings > Passwords & accounts, select that account, "
                "tap Remove account and confirm. Finish with done once no Google account is listed.")
        r = start_run(goal, "muse-wonder-1.0", 20, "deprovision")
        return {**r.summary(), "reminder": "Also review https://myaccount.google.com/device-activity"}

    frames: dict[str, dict] = {"": {"at": 0.0, "jpeg": b""}}  # live-view cache per phone ("" is the shared emulator)
    frame_locks: dict[str, threading.Lock] = {"": threading.Lock()}
    frame, frame_lock = frames[""], frame_locks[""]

    def owner_frame(device=device, secure_fallback: bool = True):
        """The picture for the owner's live view. Password and purchase screens are secure windows that Android blanks for screenshots; the owner still
        needs to see them to type, so the view falls back to the emulator's own picture. The agent never gets this (its screenshots stay blank there)."""
        try:
            img = device.frame() if hasattr(device, "frame") else Image.open(io.BytesIO(device.screenshot()))
        except Exception:
            img = None
        if secure_fallback and (img is None or is_blank(img)) and hasattr(device, "host_frame"):  # only the emulator draws a window the owner can read
            try:
                img = device.host_frame()
            except Exception:
                pass  # not an emulator: keep what we have
        if img is None:
            raise HTTPException(503, "Could not read the screen")
        return img

    @app.get("/api/device/screenshot", dependencies=[Depends(auth)])
    def screenshot(phone: str | None = Query(default=None)):
        """The live view of a phone: the shared emulator, or `?phone=games` (watch only)."""
        lane = f"virtual:{phone}" if phone else ""
        dev, lc = jobs.pair(lane) if phone else (device, lifecycle)
        if phone and dev is None:
            raise HTTPException(404, "unknown phone")
        if lc and lc.power != "on":
            raise HTTPException(409, "The phone is not on")  # no touch(): watching never keeps it awake
        cache, lock = frames.setdefault(lane, {"at": 0.0, "jpeg": b""}), frame_locks.setdefault(lane, threading.Lock())
        # One capture at a time and a short cache, so live viewers never compete with the agent for ADB.
        with lock:
            # Fresh frames while the owner is driving the phone; slower when the agent is working, so viewers never starve it.
            ttl = (0.1 if jobs.held else 2.5 if active() and not state["active"].pending else 0.8) if not phone else 1.0
            if time.time() - cache["at"] > ttl:
                img = owner_frame(dev, secure_fallback=not phone).convert("RGB")
                img.thumbnail((1200, 540) if img.width > img.height else (540, 1200))  # landscape frames keep the same short side
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=70)
                cache.update(at=time.time(), jpeg=buf.getvalue())
            jpeg = cache["jpeg"]
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def stale() -> None:
        frame["at"] = 0.0  # the next viewer request captures the result of the action

    @app.post("/api/device/rotate", dependencies=[Depends(auth)])
    def rotate(body: Rotate):
        takeover_allowed()
        device.rotate(body.orientation)
        stale()
        return {"ok": True, "orientation": device.orientation()}

    @app.websocket("/api/device/touch")
    async def touch_socket(ws: WebSocket):
        """Live touch stream for follow-the-finger dragging: {"t": "down|move|up", "fx", "fy"}. Consecutive moves are collapsed so the
        phone always gets the newest position instead of falling behind."""
        given = ws.query_params.get("token", "")
        if token and not ((given and hmac.compare_digest(given.encode(), token.encode())) or (given and store.find_device(given))):
            await ws.close(code=1008)
            return
        await ws.accept()
        inbox: asyncio.Queue = asyncio.Queue()

        async def reader() -> None:
            try:
                while True:
                    inbox.put_nowait(await ws.receive_json())
            except (WebSocketDisconnect, RuntimeError, ValueError):
                inbox.put_nowait(None)

        task, down = asyncio.create_task(reader()), None
        loop = asyncio.get_running_loop()
        try:
            while True:
                ev = await inbox.get()
                while ev is not None and ev.get("t") == "move" and not inbox.empty() and inbox._queue[0] is not None and inbox._queue[0].get("t") == "move":
                    ev = inbox.get_nowait()  # a newer move supersedes this one
                if ev is None:
                    break
                try:
                    kind, fx, fy = ev["t"], float(ev["fx"]), float(ev["fy"])
                    if kind not in ("down", "move", "up") or not (0 <= fx <= 1 and 0 <= fy <= 1) or (kind != "down" and down is None):
                        continue
                    if kind == "down":
                        takeover_allowed()
                    w, h = await loop.run_in_executor(None, device.size) if down is None else down["size"]
                    x, y = round(fx * w), round(fy * h)
                    await loop.run_in_executor(None, device.touch, kind, x, y)
                    down = {"size": (w, h), "at": (x, y)} if kind != "up" else None
                    if kind == "up":
                        stale()
                except Exception:  # a bad event or a failed injection must not end the stream
                    continue
        finally:
            task.cancel()
            if down is not None:  # the browser went away mid-drag: lift the finger
                device.touch("up", *down["at"])  # synchronous on purpose: a cancelled task must still lift the finger

    @app.post("/api/device/tap", dependencies=[Depends(auth)])
    def tap(body: Tap):
        takeover_allowed()
        w, h = device.size()
        device.tap(round(body.fx * w), round(body.fy * h))
        stale()
        return {"ok": True}

    @app.post("/api/device/type", dependencies=[Depends(auth)])
    def type_text(body: Text):
        takeover_allowed()
        device.type_text(body.text)
        stale()
        return {"ok": True}

    @app.post("/api/device/key", dependencies=[Depends(auth)])
    def key(body: Key):
        takeover_allowed()
        device.key(KEYCODES[body.key])
        stale()
        return {"ok": True}

    @app.post("/api/device/swipe", dependencies=[Depends(auth)])
    def swipe(body: Swipe):
        takeover_allowed()
        w, h = device.size()
        if body.fx1 is not None and None not in (body.fy1, body.fx2, body.fy2):
            device.swipe(round(body.fx1 * w), round(body.fy1 * h), round(body.fx2 * w), round(body.fy2 * h), body.ms)
        elif body.direction:
            cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
            d = {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
                 "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}[body.direction]
            device.swipe(*d)
        else:
            raise HTTPException(422, "give a direction or fx1, fy1, fx2, fy2")
        stale()
        return {"ok": True}

    phones = PhoneHub()

    @app.websocket("/api/phone/ws")
    async def phone_socket(ws: WebSocket):
        """The Mobile Agent app on the owner's real phone: it dials in with its device token and then answers requests (agent/remote.py)."""
        given = ws.query_params.get("token", "")
        dev = store.find_device(given) if given else None
        admin = bool(token and given and hmac.compare_digest(given.encode(), token.encode()))
        if token and not (dev or admin):
            await ws.close(code=1008)
            return
        await phones.serve(ws, dev["id"] if dev else "local")

    @app.get("/api/phones", dependencies=[Depends(auth)])
    def list_phones():
        return phones.listing()

    db = db or Database(":memory:")
    hub = Hub()
    vault = Vault(db, vault_key or secrets.token_bytes(32))  # an in-memory db gets a throwaway key
    jobs = JobManager(db, hub, device, model_factory, data_dir=data_dir, settle=settle, external_busy=lambda: bool(active()), vault=vault)
    orch = Orchestrator(db, hub, model_factory, jobs, vault)
    jobs.orch = orch
    jobs.phones = phones
    for name, (vdev, vlc) in (virtuals or {}).items():
        jobs.virtuals[name] = (vdev, vlc)
        vlc.busy = lambda lane=f"virtual:{name}": jobs.busy(lane)  # a running game task keeps its phone awake
        if name == "games":
            ensure_games_bot(db, hub, name)
    phones.on_change = lambda: [hub.publish(b["id"], {"type": "refresh", "what": "phone"}) for b in db.list_bots()]
    phones.on_connect = lambda pid, info: ensure_phone_bot(db, hub, pid, info)
    app.state.phones = phones
    if lifecycle:
        jobs.lifecycle = lifecycle
        lifecycle.busy = lambda: jobs.busy("") or jobs.held or bool(active())  # an active task or owner control keeps the phone awake
    scheduler = Scheduler(db, orch)
    orch.routines = scheduler
    scheduler.on_change = lambda bot_id: hub.publish(bot_id, {"type": "refresh", "what": "routines"})
    vault.on_change = lambda: [hub.publish(b["id"], {"type": "refresh", "what": "vault"}) for b in db.list_bots()]
    if not [b for b in db.list_bots() if b["kind"] == "bot"]:  # the master and the phones' bots do not count: there is always an ordinary assistant
        db.create_bot("Assistant", "")
    watcher = Watcher(db, hub)
    if master:
        ensure_master(db, hub)
    register_chat(app, auth, db, hub, jobs, orch, vault, scheduler)
    app.state.db, app.state.jobs, app.state.orch, app.state.vault, app.state.scheduler, app.state.hub, app.state.watcher = db, jobs, orch, vault, scheduler, hub, watcher
    jobs.start()
    if start_scheduler:
        scheduler.start()
        watcher.start()
        if lifecycle:
            lifecycle.start()
        for _, vlc in (virtuals or {}).values():
            vlc.start()
    return app


def prepare_with_retries(device, tries: int = 10) -> None:
    """The light screen profile needs the window service, which can take a few seconds after a boot or a long freeze."""
    for i in range(tries):
        try:
            device.prepare()
            return
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3)


def default_serial(env_serial: str | None, emulator_available: bool) -> str | None:
    """Every adb call names its phone: with the games phone attached to the same adb server, an unnamed call fails with "more than one device".
    The shared phone is ANDROID_SERIAL, else the local emulator; a real USB phone with no emulator on this computer stays the only device."""
    return env_serial or ("emulator-5554" if emulator_available else None)


def create_default_app() -> FastAPI:
    from agent.device import Device
    from agent.lifecycle import EmulatorBackend, Lifecycle
    from agent.virtual import GAMES, LimaRedroidBackend, SERIAL as GAMES_SERIAL
    from agent.model import MetaClient, load_env
    load_env()
    device = Device(default_serial(os.environ.get("ANDROID_SERIAL"), EmulatorBackend.available()))
    light = os.environ.get("PHONE_PROFILE", "light") == "light"
    lifecycle = None
    if os.environ.get("PHONE_SLEEP", "on") != "off" and EmulatorBackend.available():
        def ready(cold: bool) -> None:  # after a boot or a long sleep the cached connection and settings are stale
            device.reconnect()
            if cold and light:
                prepare_with_retries(device)
        lifecycle = Lifecycle(EmulatorBackend(device.base), on_ready=ready)
        if lifecycle.power == "off":
            lifecycle.wake()  # the phone is not running: boot it now, the page shows "Starting the phone"
    if (lifecycle is None or lifecycle.power == "on") and light:
        try:
            if device.is_emulator():
                prepare_with_retries(device)
        except Exception as e:  # a phone that is still settling must not stop the server from starting
            print(f"Could not apply the light profile yet ({type(e).__name__}); continuing.", flush=True)
    virtuals = {}
    if os.environ.get("GAMES_PHONE", "on") != "off" and LimaRedroidBackend.available():  # the games phone exists on this computer; nothing is started yet
        games = Device(GAMES_SERIAL)
        glc = Lifecycle(LimaRedroidBackend(), on_ready=lambda cold: games.reconnect())  # a new boot or a long freeze: forget cached connections
        glc.last_boot_s = 15  # a cold start (the VM, then the container) takes about 15 s: the progress bar should not promise a minute
        virtuals[GAMES] = (games, glc)
    app = create_app(device, lambda m: MetaClient(m),
                     token=os.environ.get("APP_TOKEN") or None, store=Store(os.environ.get("STATE_FILE", "data/state.json")),
                     db=Database(os.environ.get("DB_FILE", "data/app.db")), vault_key=load_key(), start_scheduler=True, master=True,
                     lifecycle=lifecycle, virtuals=virtuals)
    print(f"Pairing code (valid {PAIR_TTL // 60} min): {app.state.issue_pair_code()}", flush=True)
    return app


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    load = create_default_app()
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("APP_TOKEN"):
        raise SystemExit("Refusing to listen on a public address without APP_TOKEN set")
    uvicorn.run(load, host=host, port=int(os.environ.get("PORT", "8000")))
