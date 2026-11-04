"""Phone gateway: the small door the QR flow opens on the local network so a real phone can download the app and pair.

The main app stays on 127.0.0.1. This second app listens on 0.0.0.0 ONLY while the owner has switched it on, and it exposes nothing but:
  GET  /get/<token>       a mobile page: what the app does, the APK (size and SHA-256), the steps, an "open in app" link
  GET  /apk/<token>       the APK itself (the token allows 3 downloads and expires after 10 minutes)
  POST /api/pair          exchange a pairing code for a device token (same codes and throttle as the main app)
  WS   /api/phone/ws      the phone's own connection (device token)
  GET  /api/me/history    the phone bot's recent tasks (device token)
  POST /api/me/revoke     the phone disconnects itself and its token stops working (device token)
Everything else is a 404 with no body. Tokens are random, compared in constant time, never logged. HTTP only: use it on a Wi-Fi you trust, or use the USB
route (`adb reverse`) from docs/real-phone-test.md.

Two clocks: the PAIRING WINDOW (landing page, APK, pairing) closes after 15 minutes, or 2 minutes after a phone pairs. The LISTENER (the socket on the LAN)
stays on while a phone is connected through it, and closes 2 minutes after the last such phone leaves, so a paired phone keeps working without the door staying open.
"""
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket

from agent.logs import AccessLog, audit
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

TOKEN_TTL_S = 600
MAX_DOWNLOADS = 3
MAX_PAGE_VIEWS = 30
WINDOW_S = 900
AFTER_PAIR_S = 120
IDLE_S = 120
RATE_PER_MIN = 120
FAIL_LIMIT = 10
FAIL_LOCK_S = 60
APK_NAME = "mobile-agent.apk"
SECRET_RE = re.compile(r"dev_[A-Za-z0-9_\-]{16,}|[A-Za-z0-9_\-]{40,}")


def lan_address() -> str | None:
    """The computer's address on the local network (no packet is sent: a UDP socket only picks the route). None when there is no private LAN address."""
    forced = os.environ.get("PHONE_GATEWAY_ADVERTISE_IP", "").strip()  # in Docker the container sees only its bridge address: the owner names the host's LAN address
    if forced:
        try:
            a = ipaddress.ip_address(forced)
        except ValueError:
            return None
        return forced if a.is_private and not a.is_loopback and not a.is_link_local else None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ip if addr.is_private and not addr.is_loopback and not addr.is_link_local else None


def apk_info(path: Path) -> dict:
    """Is the APK there, how big, and its SHA-256 (cached by size and modification time)."""
    try:
        st = path.stat()
    except OSError:
        return {"present": False, "size": 0, "sha256": ""}
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _SHA.get(key)
    if hit is None:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _SHA.clear()
        hit = _SHA[key] = h.hexdigest()
    return {"present": True, "size": st.st_size, "sha256": hit}


_SHA: dict = {}


def hide_secrets(text: str) -> str:
    return SECRET_RE.sub("[hidden]", text or "")


def job_apps(trace_dir: str | None) -> list[str]:
    """Packages the agent opened during a job, read from its step log (never screenshots or typed text)."""
    out: list[str] = []
    if not trace_dir:
        return out
    for f in sorted(Path(trace_dir).glob("step*.json"))[:40]:
        try:
            act = (json.loads(f.read_text()).get("reply") or {}).get("action") or {}
        except (OSError, ValueError):
            continue
        if act.get("type") == "open_app" and act.get("package") and act["package"] not in out:
            out.append(str(act["package"]))
    return out


def phone_history(db, mask: Callable[[str], str], dev: dict, limit: int = 30) -> list[dict]:
    """The recent tasks of the phone bot that belongs to this device: time, goal, status, a short result, the apps used. Masked of addresses and tokens."""
    bot = db.phone_bot(dev["id"])
    if not bot:
        return []
    rows = []
    for j in db.jobs(bot["id"], limit):
        rows.append({"id": j["id"], "at": j.get("started"), "ended": j.get("ended"), "status": j["status"],
                     "goal": hide_secrets(mask(j["goal"]))[:160], "result": hide_secrets(mask(j.get("result") or ""))[:240],
                     "apps": job_apps(j.get("trace_dir"))})
    return rows


class LinkTokens:
    """One-time links: random 32-byte tokens kept only as hashes, expiring, with download and view caps, compared in constant time."""
    def __init__(self, clock=time.time, ttl: float = TOKEN_TTL_S):
        self.clock, self.ttl = clock, ttl
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._items[self._hash(token)] = {"exp": self.clock() + self.ttl, "downloads": 0, "views": 0}
        return token

    def _prune(self) -> None:
        now = self.clock()
        for h in [h for h, it in self._items.items() if it["exp"] <= now]:
            self._items.pop(h)

    def _find(self, token: str) -> dict | None:
        h = self._hash(token or "")
        found = None
        for key, it in self._items.items():  # compare against every live token: no early exit
            if hmac.compare_digest(key, h):
                found = it
        return found if found and found["exp"] > self.clock() else None

    def view(self, token: str) -> bool:
        with self._lock:
            it = self._find(token)
            if not it or it["views"] >= MAX_PAGE_VIEWS:
                return False
            it["views"] += 1
            return True

    def download(self, token: str) -> bool:
        with self._lock:
            it = self._find(token)
            if not it or it["downloads"] >= MAX_DOWNLOADS:
                return False
            it["downloads"] += 1
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class Limiter:
    """Per-address request rate and a lock after repeated bad tokens."""
    def __init__(self, clock=time.time):
        self.clock = clock
        self.hits: dict[str, list[float]] = {}
        self.fails: dict[str, list[float]] = {}
        self.locked: dict[str, float] = {}

    def allow(self, who: str) -> bool:
        now = self.clock()
        if self.locked.get(who, 0) > now:
            return False
        recent = [t for t in self.hits.get(who, []) if now - t < 60]
        recent.append(now)
        self.hits[who] = recent
        return len(recent) <= RATE_PER_MIN

    def fail(self, who: str) -> None:
        now = self.clock()
        recent = [t for t in self.fails.get(who, []) if now - t < FAIL_LOCK_S] + [now]
        self.fails[who] = recent
        if len(recent) >= FAIL_LIMIT:
            self.locked[who] = now + FAIL_LOCK_S
            self.fails[who] = []


class Listener:
    """The real socket: a uvicorn server in its own thread. Replaced by a fake in tests."""
    def __init__(self, app: FastAPI, host: str, port: int):
        self.app, self.host, self.port = app, host, port
        self.server = None
        self.thread: threading.Thread | None = None
        self.error = ""

    def start(self) -> None:
        import uvicorn
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", access_log=False, lifespan="off", log_config=None)  # log_config=None: uvicorn must not replace the app's log files
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, daemon=True, name="phone-gateway")
        self.thread.start()
        end = time.time() + 5
        while time.time() < end and not self.server.started and self.thread.is_alive():
            time.sleep(0.05)
        if not self.server.started:
            self.error = self.error or "could not listen on that port"
            raise OSError(self.error)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as e:  # a busy port ends the server with SystemExit
            self.error = f"could not listen on port {self.port}: {e}"

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


DISCOVERY_PORT = 8767
DISCOVERY_MAGIC = b"MOBILE_AGENT_DISCOVER v1"
DISCOVERY_RATE = 20  # answers per source address per 10 seconds


def computer_name() -> str:
    return re.sub(r"[^A-Za-z0-9 ._\-]", "", socket.gethostname().split(".")[0])[:40] or "this computer"


class Discovery:
    """Lets the phone app find this computer on the Wi-Fi so the owner types only the pairing code. A UDP socket answers one fixed question
    (`MOBILE_AGENT_DISCOVER v1`) with {app, v, port, name}: no address (the phone uses the reply's source address), no code, no token.
    It answers only while the pairing window is open (`active()`), only senders on a private network, and only a few times a second per sender."""
    def __init__(self, port: int, active: Callable[[], bool], clock=time.time, udp_port: int = DISCOVERY_PORT, allow_loopback: bool = False):
        self.port, self.active, self.clock, self.udp_port, self.allow_loopback = port, active, clock, udp_port, allow_loopback
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.seen: dict[str, list[float]] = {}
        self.error = ""

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.udp_port))
        except OSError as e:  # best effort: without it the phone falls back to scanning, or the owner types the address
            s.close()
            self.error = str(e)
            logging.getLogger("agent.gateway").warning("discovery could not listen on UDP %d: %s", self.udp_port, e)
            return
        s.settimeout(0.5)
        self.sock = s
        self.thread = threading.Thread(target=self._loop, daemon=True, name="phone-discovery")
        self.thread.start()

    def _ok_sender(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return (a.is_private and not a.is_loopback and not a.is_link_local) or (self.allow_loopback and a.is_loopback)

    def _rate_ok(self, ip: str) -> bool:
        now = self.clock()
        recent = [t for t in self.seen.get(ip, []) if now - t < 10] + [now]
        self.seen[ip] = recent
        if len(self.seen) > 500:
            self.seen = {k: v for k, v in self.seen.items() if v and now - v[-1] < 10}
        return len(recent) <= DISCOVERY_RATE

    def _loop(self) -> None:
        s = self.sock
        while s is not None and self.sock is s:
            try:
                data, (ip, port) = s.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != DISCOVERY_MAGIC or not self.active() or not self._ok_sender(ip) or not self._rate_ok(ip):
                continue
            try:
                s.sendto(json.dumps({"app": "mobile-agent", "v": 1, "port": self.port, "name": computer_name()}).encode(), (ip, port))
            except OSError:
                pass

    def stop(self) -> None:
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass
        if self.thread:
            self.thread.join(timeout=2)


class PhoneGateway:
    def __init__(self, store, phones, do_pair: Callable[[str, str], tuple[str, str]], issue_code: Callable[[], str],
                 history: Callable[[dict], list], revoke: Callable[[dict], None], apk_path: Path, port: int = 8765,
                 host: str = "0.0.0.0", clock=time.time, listener_factory=Listener, lan: Callable[[], str | None] = lan_address, discovery_factory=Discovery):
        self.store, self.phones, self.do_pair, self.issue_code, self.history, self.revoke = store, phones, do_pair, issue_code, history, revoke
        self.apk_path, self.port, self.host, self.clock, self.listener_factory, self.lan = Path(apk_path), port, host, clock, listener_factory, lan
        self.tokens, self.limiter = LinkTokens(clock), Limiter(clock)
        self.app = self._build()
        self.listener = None
        self.discovery_factory, self.discovery = discovery_factory, None
        self.window_until = 0.0
        self.idle_since: float | None = None
        self.token = ""
        self.code = ""
        self.opened_at = 0.0
        self.paired: list[dict] = []
        self.error = ""
        self._lock = threading.Lock()
        self._ticker: threading.Thread | None = None

    # --- state for the web dialog (the main app, loopback) ---------------------------------------------------------
    @property
    def listening(self) -> bool:
        return self.listener is not None

    def window_open(self) -> bool:
        return self.listening and self.clock() < self.window_until

    def address(self) -> str | None:
        ip = self.lan()
        return f"{ip}:{self.port}" if ip else None

    def state(self) -> dict:
        now = self.clock()
        addr = self.address()
        open_ = self.window_open()
        return {"opened_at": self.opened_at, "listening": self.listening, "window": open_, "window_left": max(0, round(self.window_until - now)) if open_ else 0,
                "address": addr, "lan": bool(addr), "port": self.port, "apk": apk_info(self.apk_path),
                "url": f"http://{addr}/get/{self.token}" if open_ and addr and self.token else None,
                "code": self.code if open_ else None, "error": self.error,
                "paired": list(self.paired), "connected": [p for p in self.phones.listing() if p.get("via") == "gateway"]}

    def keeps_listening(self) -> bool:
        """A paired phone must always be able to come back (a restart of this app, a Wi-Fi change, a phone that slept): the listener stays on while any phone is paired.
        Only the phone's own socket (device token) and its history answer then; the page, the app file and pairing need the owner's open window.
        Off with PHONE_GATEWAY_PERSIST=off, or by removing the paired phones."""
        if os.environ.get("PHONE_GATEWAY_PERSIST", "on").strip().lower() in ("off", "0", "no"):
            return False
        try:
            return bool(self.store.devices())
        except Exception:
            return False

    def restore(self) -> None:
        """At start-up: listen again for the phones that are already paired (no pairing window, no link, no code)."""
        with self._lock:
            if self.listener is not None or not self.keeps_listening() or self.lan() is None:
                return
            try:
                listener = self.listener_factory(self.app, self.host, self.port)
                listener.start()
            except OSError as e:
                self.error = str(e) or f"could not listen on port {self.port}"
                return
            self.listener = listener
            self.idle_since = None
            self._start_ticker()
        audit("phone door listening for paired phones", port=self.port)

    def open(self) -> dict:
        """Switch the door on: start listening if needed, open the pairing window, make a fresh link and code."""
        with self._lock:
            self.error = ""
            if self.lan() is None:
                self.error = "No local network address was found. Connect this computer to Wi-Fi, or use the USB route below."
                return self.state()
            if self.listener is None:
                try:
                    listener = self.listener_factory(self.app, self.host, self.port)
                    listener.start()
                    self.listener = listener
                except OSError as e:
                    self.error = str(e) or f"could not listen on port {self.port}"
                    return self.state()
            if self.discovery is None:
                self.discovery = self.discovery_factory(self.port, self.window_open)
                self.discovery.start()
            self.tokens.clear()
            self.token = self.tokens.issue()
            self.code = self.issue_code()
            self.opened_at = self.clock()
            self.window_until = self.opened_at + WINDOW_S
            self.paired = []
            self.idle_since = None
            self._start_ticker()
        return self.state()

    def close(self) -> dict:
        """'Turn off': the pairing window ends at once (link, code, app file, discovery). Phones that are already paired keep their way in; with none paired the door closes completely."""
        with self._lock:
            if self.listener is not None and self.keeps_listening():
                self.window_until = 0.0
                self.token = self.code = ""
                self.tokens.clear()
                discovery, self.discovery = self.discovery, None
                if discovery:
                    discovery.stop()
            else:
                self._stop()
        audit("phone gateway closed")
        return self.state()

    def _stop(self) -> None:
        discovery, self.discovery = self.discovery, None
        if discovery:
            discovery.stop()
        listener, self.listener = self.listener, None
        self.window_until = 0.0
        self.token = self.code = ""
        self.tokens.clear()
        if listener:
            try:
                listener.stop()
            except Exception:
                pass

    def _start_ticker(self) -> None:
        if self._ticker and self._ticker.is_alive():
            return
        self._ticker = threading.Thread(target=self._loop, daemon=True, name="phone-gateway-timer")
        self._ticker.start()

    def _loop(self) -> None:
        while self.listening:
            time.sleep(2)
            self.tick()

    def tick(self) -> None:
        """Close the window when it is over; close the listener 2 minutes after the last gateway phone left."""
        with self._lock:
            if not self.listening:
                return
            now = self.clock()
            if self.window_until and now >= self.window_until:
                self.window_until = 0.0
                self.token = self.code = ""
                self.tokens.clear()
            if any(p.get("via") == "gateway" for p in self.phones.listing()) or self.keeps_listening():
                self.idle_since = None
            elif not self.window_open():
                self.idle_since = self.idle_since or now
                if now - self.idle_since >= IDLE_S:
                    self._stop()
            else:
                self.idle_since = None

    def _paired(self, name: str) -> None:
        """A phone paired through the door: the window closes soon, the links stop working."""
        self.paired.append({"name": name, "at": self.clock()})
        audit("phone paired through the gateway", name=name)
        self.window_until = min(self.window_until, self.clock() + AFTER_PAIR_S)
        self.tokens.clear()
        self.token = self.code = ""  # the link and the code are spent: the dialog must not show a dead QR

    # --- the allow-listed app --------------------------------------------------------------------------------------
    def _build(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.add_middleware(AccessLog, listener="gateway")
        gw = self

        def who(request: Request) -> str:
            return request.client.host if request.client else "?"

        def guard(request: Request) -> str:
            w = who(request)
            if not gw.limiter.allow(w):
                raise HTTPException(429, "slow down")
            return w

        def device(authorization: str | None) -> dict:
            token = (authorization or "").removeprefix("Bearer ").strip()
            dev = gw.store.find_device(token) if token else None
            if not dev:
                raise HTTPException(401, "invalid token")
            return dev

        def headers() -> dict:
            return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

        @app.get("/get/{token}")
        def landing(token: str, request: Request):
            w = guard(request)
            if not gw.window_open() or not gw.tokens.view(token):
                gw.limiter.fail(w)
                return PlainTextResponse("", status_code=404)
            addr = gw.address() or ""
            return HTMLResponse(landing_page(token, addr, gw.issue_code(), apk_info(gw.apk_path)),
                                headers={**headers(), "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"})

        @app.get("/apk/{token}")
        def apk(token: str, request: Request):
            w = guard(request)
            if not gw.window_open() or not gw.tokens.download(token):
                gw.limiter.fail(w)
                return PlainTextResponse("", status_code=404)
            if not gw.apk_path.exists():
                return PlainTextResponse("", status_code=404)
            return FileResponse(gw.apk_path, media_type="application/vnd.android.package-archive", filename=APK_NAME, headers=headers())

        @app.get("/api/hello")
        async def hello(request: Request):  # what the phone app's scan looks for when broadcast is blocked: the same four fields discovery answers with
            guard(request)
            return {"app": "mobile-agent", "v": 1, "port": gw.port, "name": computer_name()}

        @app.post("/api/pair")
        async def pair(request: Request):
            guard(request)
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(422, "bad request")
            code, name = str(body.get("code", ""))[:20], str(body.get("name", "phone")).strip()[:60] or "phone"
            if len(code) < 4:
                raise HTTPException(422, "bad request")
            did, tok = gw.do_pair(code, name)
            with gw._lock:
                gw._paired(name)
            return JSONResponse({"device_id": did, "token": tok}, headers=headers())

        @app.websocket("/api/phone/ws")
        async def phone_ws(ws: WebSocket):
            given = ws.query_params.get("token", "")
            dev = gw.store.find_device(given) if given else None
            if not dev or not gw.limiter.allow(ws.client.host if ws.client else "?"):
                await ws.close(code=1008)
                return
            await gw.phones.serve(ws, dev["id"], via="gateway")

        @app.get("/api/me/history")
        def me_history(request: Request, authorization: str | None = Header(default=None)):
            guard(request)
            return JSONResponse(gw.history(device(authorization)), headers=headers())

        @app.post("/api/me/revoke")
        def me_revoke(request: Request, authorization: str | None = Header(default=None)):
            guard(request)
            gw.revoke(device(authorization))
            return JSONResponse({"ok": True}, headers=headers())

        return app


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def landing_page(token: str, address: str, code: str, apk: dict) -> str:
    """The page a phone sees after scanning the QR. Small, dark, no external requests."""
    server = f"http://{address}"
    deep = f"mobileagent://pair?server={server}&code={code}"
    e = html.escape
    download = (f'<a class="btn" href="/apk/{e(token)}" download>Download the app ({_mb(apk["size"])})</a>'
                f'<p class="mono small">SHA-256 {e(apk["sha256"])}</p>') if apk.get("present") else \
        '<p class="warn">The app file is not on the computer yet. On the computer run <span class="mono">scripts/build_apk.sh</span>, then scan again.</p>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mobile Agent for Android</title><style>
:root{{color-scheme:dark;--ink-1:#0b0d14;--ink-2:#11141c;--ink-3:#1a1e28;--line:#eef0f524;--text:#eef0f5;--text-2:#a3a9b8;--amber:#f0a43a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--ink-1);color:var(--text);font:400 16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px 18px 48px;max-width:560px;margin-inline:auto}}
h1{{font-size:24px;margin:0 0 6px}}h2{{font-size:16px;margin:28px 0 8px}}p,li{{color:var(--text-2)}}ol{{padding-left:20px}}li{{margin:6px 0}}
.btn{{display:block;text-align:center;background:var(--text);color:var(--ink-1);font-weight:600;padding:14px;border-radius:10px;text-decoration:none;margin:14px 0 6px}}
.btn.alt{{background:var(--ink-3);color:var(--text);border:1px solid var(--line)}}.mono{{font-family:ui-monospace,Menlo,monospace;word-break:break-all}}.small{{font-size:12px}}
.card summary{{cursor:pointer}}.card{{background:var(--ink-2);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}}.warn{{color:var(--amber)}}
</style></head><body>
<h1>Mobile Agent</h1><p>Lets the assistant on your computer do tasks on this phone, in the background, when you ask it to.</p>
<div class="card"><b>What it can see and do</b><p>It reads what is on the screen, taps, types and opens apps, and it can list the apps installed here. Nothing else: no files, no photos, no messages, no contacts, no location. A notification is always shown while it is connected, with a Stop button.</p></div>
<h2>1. Get the app</h2>{download}
<h2>2. Install it</h2><ol><li>Chrome may warn that the file "can't be downloaded securely" and "might be harmful": this is a plain-HTTP link to your own computer, and the SHA-256 above is the file's fingerprint. Tap <b>Keep</b>, then <b>Download anyway</b>.</li><li>Open the downloaded file. If Android asks, allow installs from this browser.</li><li>Open Mobile Agent. The server and pairing code are filled in for you: tap <b>Open in the app</b> below, then <b>Confirm</b>.</li></ol>
<a class="btn alt" href="{e(deep)}">Open in the app</a>
<details class="card"><summary><b>If your phone says "App blocked to protect your device"</b></summary>
<p>That is Google Play Protect, not a virus warning about this app's code: in some countries it blocks any app that can operate the screen when it comes from a browser download. These ways commonly work (Google does not promise any of them):</p>
<ol><li>On the computer, in the Connect a phone window, open <b>Install from this computer</b>: it installs over a USB cable or Wi-Fi debugging, which Play Protect does not treat as a download.</li>
<li>Pause scanning for a minute: Play Store, your profile icon, Play Protect, the gear, turn off <b>Scan apps with Play Protect</b>. Install, then turn it back on.</li>
<li>If the dialog shows <b>More details</b>, choose <b>Install anyway</b>.</li></ol></details>
<h2>3. Let it work</h2><ol><li>In the app, open Accessibility settings and turn on <b>Mobile Agent</b>.</li><li>On Android 13 or newer, if the switch is grey: App info, the three dots, <b>Allow restricted settings</b>, then try again.</li><li>Allow background running when the app asks.</li></ol>
<p class="small">This page and the download are only available on your local network for a few minutes. Server: <span class="mono">{e(server)}</span></p>
</body></html>"""
