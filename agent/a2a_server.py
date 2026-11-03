"""The A2A door: its own small web app (agent card, JSON-RPC, files, docs), the switch that starts and stops it, and the webhook sender for push notifications.

The main app stays on 127.0.0.1. This one listens only while the owner has A2A switched on (and `A2A_HOST` decides where: loopback by default).
Everything except the card, the skills list and the docs page needs a client's token.
"""
import asyncio
import html
import ipaddress
import json
import os
import queue
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.logs import AccessLog, audit
from agent import a2a
from agent.a2a import AuthError, ERRORS, SKILLS, STATE_03
from agent.a2a_rpc import Service
from agent.gateway import Listener, lan_address

MAX_BODY = 60 * 1024 * 1024  # base64 of 40 MB of files plus text
MAX_STREAMS_PER_CLIENT = 5
PUSH_TRIES = 3
SECURITY = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
INLINE_TYPES = {"image/png", "image/jpeg", "image/webp", "text/plain", "application/json"}


# ---- push notifications ----------------------------------------------------------------------------------------------------------------
def push_target_problem(url: str, resolve: bool = True, allow_private: bool = False, allow_http: bool = False) -> str:
    """Why this webhook address may not be used, or "" when it may: https only, no credentials in it, and (at send time) only public addresses."""
    u = urlparse(url.strip())
    if u.scheme != "https" and not (allow_http and u.scheme == "http"):
        return "the webhook address must start with https://"
    if not u.hostname or u.username or u.password or len(url) > 1000:
        return "the webhook address is not valid"
    if not resolve:  # registering a webhook does no DNS lookup, but an address that is plainly local is refused at once
        if not allow_private:
            host = u.hostname.lower()
            try:
                local = not ipaddress.ip_address(host).is_global
            except ValueError:
                local = host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            if local:
                return "the webhook address points to a private or local network, which is not allowed"
        return ""
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return "the webhook host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not allow_private and not ip.is_global:
            return "the webhook address points to a private or local network, which is not allowed"
    return ""


class PushSender:
    """Sends task updates to the webhooks callers registered, from a worker thread. Failures are written to the audit trail and never touch the task."""

    def __init__(self, svc: Service, allow_private: bool = False, allow_http: bool = False, sleep=time.sleep, timeout: float = 5.0):
        self.svc, self.allow_private, self.allow_http, self.sleep, self.timeout = svc, allow_private, allow_http, sleep, timeout
        self.q: queue.Queue = queue.Queue(maxsize=1000)
        self.thread = None
        svc.listeners.append(self.on_event)

    def on_event(self, task: dict, event: dict) -> None:
        if not self.svc.store.push_for_task(task["id"]):
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="a2a-push")
            self.thread.start()
        try:
            self.q.put_nowait((task, event))
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            try:
                task, event = self.q.get(timeout=30)
            except queue.Empty:
                return
            for cfg in self.svc.store.push_for_task(task["id"]):
                self.send(cfg, task, event)

    def send(self, cfg: dict, task: dict, event: dict) -> bool:
        problem = push_target_problem(cfg["url"], True, self.allow_private, self.allow_http)
        if problem:
            self.svc.store.audit(cfg["client_id"], "push", task["id"], False, problem)
            return False
        body = self.svc.stream_frame(event, cfg["dialect"])  # the payload is a StreamResponse (a status or artifact update), in the dialect the caller registered with
        headers = {"Content-Type": "application/json"}
        if cfg["token"]:
            headers["X-A2A-Notification-Token"] = cfg["token"]
        if cfg["auth_credentials"]:
            headers["Authorization"] = f"{cfg['auth_scheme'] or 'Bearer'} {cfg['auth_credentials']}"
        last = ""
        for attempt in range(PUSH_TRIES):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as c:
                    r = c.post(cfg["url"], json=body, headers=headers)
                if r.status_code < 300:
                    return True
                last = f"HTTP {r.status_code}"
                if r.status_code in (301, 302, 303, 307, 308):
                    break  # never follow a redirect: it could lead somewhere private
            except httpx.HTTPError as e:
                last = type(e).__name__
            if attempt + 1 < PUSH_TRIES:
                self.sleep(2 ** attempt)
        self.svc.store.audit(cfg["client_id"], "push", task["id"], False, f"gave up: {last}")
        return False


# ---- the docs page ---------------------------------------------------------------------------------------------------------------------
def docs_page(base: str) -> str:
    e = html.escape
    pj = lambda o: e(json.dumps(o, indent=2, ensure_ascii=False))
    rows = []
    for s in SKILLS:
        rows.append(f"""<section class="skill" id="{e(s['id'])}"><h3>{e(s['name'])} <code>{e(s['id'])}</code></h3><p>{e(s['description'])}</p>
<p class="meta">Needs permission <code>{e(s['scope'])}</code>. Input: {e(', '.join(s['inputModes']))}. Output: {e(', '.join(s['outputModes']))}.</p><p>{e(s['outputs'])}</p>
<p class="meta">Examples: {e(' · '.join(s['examples']))}</p><details><summary>JSON Schema for the data part</summary><pre>{pj(s['schema'])}</pre></details></section>""")
    send = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "Open Settings and tell me the Android version"}, {"data": {"skill": "phone-task"}}]}}}
    retrieve = {"jsonrpc": "2.0", "id": 2, "method": "SendMessage", "params": {"message": {"messageId": "m-2", "role": "ROLE_USER", "parts": [{"data": {"skill": "retrieve-context", "query": "android version", "limit": 3}}]}}}
    states = "".join(f"<tr><td><code>{e(k)}</code></td><td><code>{e(v)}</code></td></tr>" for k, v in STATE_03.items())
    errors = "".join(f"<tr><td>{c}</td><td>{e(n)}</td></tr>" for n, c in ERRORS.items())
    curl = lambda body, extra="": ("curl -s " + base + '/a2a -H "Authorization: Bearer $A2A_TOKEN" -H "Content-Type: application/json" -H "A2A-Version: 1.0" ' + extra + "-d '" + json.dumps(body) + "'")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mobile Agent over A2A</title>
<style>:root{{--bg:#fff;--fg:#1b1d22;--mut:#5b6170;--line:#dde0e7;--card:#f6f7f9;--acc:#1f6feb}}@media(prefers-color-scheme:dark){{:root{{--bg:#101216;--fg:#e8eaee;--mut:#9aa1b0;--line:#272b33;--card:#171a20;--acc:#6ea8ff}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,sans-serif;margin:0}}main{{max-width:860px;margin:0 auto;padding:32px 20px 80px}}h1{{font-size:28px}}h2{{margin-top:40px;border-top:1px solid var(--line);padding-top:20px}}
h3{{margin-bottom:4px}}code,pre{{font:13px/1.5 ui-monospace,Menlo,monospace}}code{{background:var(--card);padding:1px 5px;border-radius:4px}}pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}}
.meta{{color:var(--mut);font-size:14px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;font-size:14px}}a{{color:var(--acc)}}.skill{{margin:20px 0}}</style></head><body><main>
<h1>Mobile Agent over A2A</h1>
<p>An assistant that operates real Android phones in the cloud. Other agents can hire it through the open <a href="https://a2a-protocol.org" rel="noopener">Agent2Agent</a> protocol (version 1.0, and 0.3 for older clients). It does phone tasks, checks links and APK files, explains with pictures, answers from a shared memory, and lets you search and add to that memory.</p>
<p>Agent card: <a href="{e(base)}/.well-known/agent-card.json"><code>{e(base)}/.well-known/agent-card.json</code></a> (add <code>?version=0.3</code> for the 0.3 card). Endpoint: <code>{e(base)}/a2a</code> (JSON-RPC over HTTP, SSE for streaming). Skills as JSON Schema: <a href="{e(base)}/a2a/skills"><code>/a2a/skills</code></a>.</p>
<h2>Connect</h2><ol><li>The owner creates a caller for you on the Agents page and gives you a token (shown once, starts with <code>a2a_</code>), and the permissions you get: <code>ask</code>, <code>phone</code>, <code>context-write</code>.</li>
<li>Send <code>Authorization: Bearer &lt;token&gt;</code> on every request except the card, <code>/a2a/skills</code> and this page.</li><li>Send <code>A2A-Version: 1.0</code> (0.3 callers use the 0.3 method names such as <code>message/send</code>; the method name decides the dialect and the reply uses the same one).</li></ol>
<h2>Skills</h2>{''.join(rows)}
<h2>Examples</h2><p>A phone task:</p><pre>{e(curl(send))}</pre><p>The same, streamed (<code>SendStreamingMessage</code>), prints one JSON-RPC frame per event:</p><pre>{e(curl({**send, 'method': 'SendStreamingMessage'}, '-N '))}</pre>
<p>Ask for the task later:</p><pre>{e(curl({'jsonrpc': '2.0', 'id': 3, 'method': 'GetTask', 'params': {'id': '&lt;task id&gt;', 'historyLength': 10}}))}</pre><p>Search the shared knowledge (answered at once, no model):</p><pre>{e(curl(retrieve))}</pre>
<p>Files go in as parts with <code>raw</code> (base64), <code>filename</code> and <code>mediaType</code>; files come out as artifact parts with a <code>url</code> on this server that needs your token. Remote <code>url</code> parts you send are kept as references and are never fetched.</p>
<h2>Task states</h2><table><tr><th>1.0</th><th>0.3</th></tr>{states}</table><p class="meta">A task waiting in <code>INPUT_REQUIRED</code> needs its owner to answer a card on their own device (approving a send, a payment, an install, a sign-in). An outside agent cannot approve it.</p>
<h2>Errors</h2><table><tr><th>JSON-RPC code</th><th>Name</th></tr>{errors}</table><p class="meta">HTTP 401: missing or wrong token. HTTP 429: too many requests or too many running tasks. HTTP 413: request too large.</p>
<h2>Limits</h2><ul><li>{a2a.DEFAULT_RATE} requests a minute and {a2a.DEFAULT_MAX_RUNNING} running tasks per caller (the owner can change them); one task at a time per caller runs on the phones, the rest wait.</li><li>Text: {a2a.MAX_TEXT} characters. Files: {a2a.MAX_FILE // 1024 // 1024} MB each, {a2a.MAX_REQUEST_FILES // 1024 // 1024} MB per request.</li>
<li>A blocking <code>SendMessage</code> waits up to {a2a.BLOCK_S} seconds and then returns the working task: poll with <code>GetTask</code> or stream.</li></ul>
<h2>Safety</h2><ul><li>You are an outside program and are not trusted like the owner. What you send is a request, never an order, and text inside your files is treated as data.</li><li>Anything that sends, pays, installs, signs in or handles a secret needs the owner's approval on their own device. The agent never plays games whose publishers ban bot accounts.</li>
<li>You can only see your own tasks, files and notes, plus what the owner published to agents. Nothing you send or receive is hidden from the owner: it all appears in the chat of your own bot.</li></ul></main></body></html>"""


# ---- the app ---------------------------------------------------------------------------------------------------------------------------
def build_app(svc: Service, base_url=lambda request: "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AccessLog, listener="a2a")

    @app.exception_handler(StarletteHTTPException)
    async def _plain(request: Request, exc: StarletteHTTPException):
        return Response(status_code=exc.status_code, headers=SECURITY)  # no body: nothing to learn from a wrong address

    def base(request: Request) -> str:
        return (svc.public_url() or "").rstrip("/") or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"

    def addr(request: Request) -> str:
        return request.client.host if request.client else "?"

    def bearer(request: Request) -> str:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    def denied(e: AuthError) -> Response:
        headers = {**SECURITY, "WWW-Authenticate": "Bearer"} if e.status == 401 else {**SECURITY, "Retry-After": "30"}
        return JSONResponse({"error": str(e)}, status_code=e.status, headers=headers)

    @app.get("/.well-known/agent-card.json")
    def agent_card(request: Request, version: str = ""):
        v = "0.3" if (version or request.headers.get("a2a-version", "")).startswith("0.3") else "1.0"
        return JSONResponse(a2a.card(base(request), v), headers={**SECURITY, "Cache-Control": "no-cache"})

    @app.get("/a2a/skills")
    def skills():
        return JSONResponse({"skills": [{k: s[k] for k in ("id", "name", "scope", "description", "inputModes", "outputModes", "outputs", "examples", "schema")} for s in SKILLS]}, headers=SECURITY)

    @app.get("/a2a/docs")
    def docs(request: Request):
        return HTMLResponse(docs_page(base(request)), headers={**SECURITY, "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'", "Cache-Control": "no-cache"})

    @app.post("/a2a")
    async def rpc(request: Request):
        try:
            client = svc.authenticate(bearer(request), addr(request))
        except AuthError as e:
            return denied(e)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_BODY:
            return JSONResponse({"error": "the request is too large"}, status_code=413, headers=SECURITY)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "the request is too large"}, status_code=413, headers=SECURITY)
        try:
            payload = json.loads(bytes(body))
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": ERRORS["ParseError"], "message": "not valid JSON"}}, headers=SECURITY)
        try:
            kind, out = await asyncio.get_running_loop().run_in_executor(None, svc.rpc, client, payload, request.headers.get("a2a-version", ""), base(request), request.headers.get("last-event-id", ""))
        except AuthError as e:
            return denied(e)
        if kind == "json":
            return JSONResponse(out, headers=SECURITY)
        if len(svc.streams.setdefault(client["id"], [])) >= MAX_STREAMS_PER_CLIENT:
            return JSONResponse({"error": f"too many open streams (limit {MAX_STREAMS_PER_CLIENT})"}, status_code=429, headers=SECURITY)
        token = object()

        def frames():
            svc.streams[client["id"]].append(token)
            try:
                for seq, frame in out:
                    yield (f"id: {seq}\n" if seq is not None else "") + f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
            finally:
                svc.streams[client["id"]].remove(token)

        return StreamingResponse(frames(), media_type="text/event-stream", headers={**SECURITY, "X-Accel-Buffering": "no"})

    @app.get("/a2a/files/{fid}")
    def files(fid: str, request: Request):
        try:
            client = svc.authenticate(bearer(request), addr(request))
        except AuthError as e:
            return denied(e)
        row = svc.store.get_file(fid)
        if not row or row["client_id"] != client["id"]:
            return Response(status_code=404, headers=SECURITY)
        path = Path(row["path"])
        try:
            inside = path.resolve().is_relative_to(svc.data_dir.resolve()) or path.resolve().is_relative_to(Path(svc.orch.jobs.data_dir).resolve())
        except OSError:
            inside = False
        if not inside or not path.exists():
            return Response(status_code=404, headers=SECURITY)
        mt = row["media_type"] or "application/octet-stream"
        disp = "inline" if mt in INLINE_TYPES else "attachment"
        name = "".join(c for c in row["filename"] if c.isalnum() or c in "._- ") or "file"
        return FileResponse(path, media_type=mt, headers={**SECURITY, "Content-Disposition": f'{disp}; filename="{name}"', "Content-Security-Policy": "default-src 'none'; sandbox"})

    return app


# ---- the switch ------------------------------------------------------------------------------------------------------------------------
def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


class Manager:
    """Starts and stops the A2A listener; the owner's choice is kept in the database so it comes back after a restart."""

    def __init__(self, svc: Service, host: str | None = None, port: int | None = None, listener_factory=Listener, lan=lan_address, push: PushSender | None = None):
        self.svc = svc
        self.host = host or os.environ.get("A2A_HOST", "127.0.0.1")
        self.port = int(port if port is not None else os.environ.get("A2A_PORT", "8766"))
        self.listener_factory, self.lan = listener_factory, lan
        self._public = os.environ.get("A2A_PUBLIC_URL", "").rstrip("/")  # set it when the door is reached through a proxy or a tunnel
        self.listener = None
        self.error = ""
        self.lock = threading.Lock()
        self.app = build_app(svc)
        self.push = push or PushSender(svc)
        svc.public_url = self.base

    def base(self) -> str:
        if self._public:
            return self._public
        if is_loopback(self.host):
            host = "127.0.0.1"
        elif self.host in ("0.0.0.0", "::"):
            host = self.lan() or self.host
        else:
            host = self.host
        return f"http://{host}:{self.port}"

    @property
    def listening(self) -> bool:
        return self.listener is not None

    def enabled(self) -> bool:
        return self.svc.store.config("enabled") == "1"

    def start(self) -> None:
        with self.lock:
            if self.listener is not None:
                return
            self.error = ""
            if not is_loopback(self.host) and not [c for c in self.svc.store.clients() if not c["revoked"]]:
                self.error = "Create a caller first: this door is open to the network, and nobody has a token yet."
                return
            try:
                listener = self.listener_factory(self.app, self.host, self.port)
                listener.start()
                self.listener = listener
                audit("A2A door opened", host=self.host, port=self.port)
            except OSError as e:
                self.error = str(e) or f"could not listen on port {self.port}"

    def stop(self) -> None:
        with self.lock:
            listener, self.listener = self.listener, None
        if listener:
            audit("A2A door closed")
            try:
                listener.stop()
            except Exception:
                pass

    def enable(self) -> dict:
        self.svc.store.set_config("enabled", "1")
        self.start()
        self.svc.on_change()
        return self.state()

    def disable(self) -> dict:
        self.svc.store.set_config("enabled", "0")
        self.stop()
        self.svc.on_change()
        return self.state()

    def restore(self) -> None:
        if self.enabled():
            self.start()

    def state(self) -> dict:
        b = self.base()
        return {"enabled": self.enabled(), "listening": self.listening, "host": self.host, "port": self.port, "lan": not is_loopback(self.host), "error": self.error,
                "urls": {"card": f"{b}/.well-known/agent-card.json", "rpc": f"{b}/a2a", "docs": f"{b}/a2a/docs", "skills": f"{b}/a2a/skills"}}


# ---- the owner's management routes (main app, local) ---------------------------------------------------------------------------------
def register_management(app: FastAPI, auth, svc: Service, mgr: Manager) -> None:
    from fastapi import Depends, HTTPException, Query
    from pydantic import BaseModel, Field

    class NewClient(BaseModel):
        name: str = Field(min_length=1, max_length=40)
        scopes: list[str] = Field(default_factory=lambda: ["ask"])
        rate_per_min: int = Field(default=a2a.DEFAULT_RATE, ge=1, le=600)
        max_running: int = Field(default=a2a.DEFAULT_MAX_RUNNING, ge=1, le=20)

    def view() -> dict:
        tasks, total = svc.store.tasks(limit=20)
        st = mgr.state()
        return {**st, "lanAddress": mgr.lan() if st["lan"] else None, "scopes": list(a2a.SCOPES), "defaults": {"ratePerMin": a2a.DEFAULT_RATE, "maxRunning": a2a.DEFAULT_MAX_RUNNING},
                "skills": [{"id": s["id"], "name": s["name"], "scope": s["scope"]} for s in SKILLS], "clients": [svc.client_row(c) for c in svc.store.clients()],
                "tasks": [svc.task_row(t) for t in tasks], "taskTotal": total}

    @app.get("/api/a2a", dependencies=[Depends(auth)])
    def a2a_state():
        return view()

    @app.post("/api/a2a/enable", dependencies=[Depends(auth)])
    def a2a_enable():
        mgr.enable()
        return view()

    @app.post("/api/a2a/disable", dependencies=[Depends(auth)])
    def a2a_disable():
        mgr.disable()
        return view()

    @app.post("/api/a2a/clients", dependencies=[Depends(auth)])
    def a2a_create(body: NewClient):
        try:
            client, token = svc.create_client(body.name, body.scopes, body.rate_per_min, body.max_running)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        audit("A2A caller created", name=body.name, scopes=",".join(body.scopes))
        return {"client": svc.client_row(client), "token": token}  # the only time the token is shown

    @app.post("/api/a2a/clients/{cid}/rotate", dependencies=[Depends(auth)])
    def a2a_rotate(cid: str):
        token = svc.rotate_client(cid)
        if not token:
            raise HTTPException(404, "unknown caller")
        audit("A2A caller token rotated", client=cid)
        return {"client": svc.client_row(svc.store.get_client(cid)), "token": token}

    @app.delete("/api/a2a/clients/{cid}", dependencies=[Depends(auth)])
    def a2a_revoke(cid: str, purge: bool = Query(default=False)):
        """Revoke a caller: its token stops working and its running tasks are stopped; its history stays. purge=true also deletes everything stored for it."""
        if not svc.store.get_client(cid):
            raise HTTPException(404, "unknown caller")
        svc.revoke_client(cid)
        audit("A2A caller removed", client=cid, purge=purge)
        if purge:
            kn = svc.knowledge
            if kn is not None:
                kn.delete_for_client(cid)
            for f in svc.db._q("select path from a2a_files where client_id=?", (cid,)):
                p = Path(f["path"])
                if p.resolve().is_relative_to((svc.data_dir / "a2a").resolve()):
                    p.unlink(missing_ok=True)
            svc.store.purge_client(cid)
            svc.on_change()
        return {"ok": True, "purged": purge}

    @app.get("/api/a2a/tasks", dependencies=[Depends(auth)])
    def a2a_tasks(client: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200)):
        rows, total = svc.store.tasks(client or None, limit=limit)
        return {"tasks": [svc.task_row(t) for t in rows], "total": total}

    @app.get("/api/a2a/tasks/{tid}", dependencies=[Depends(auth)])
    def a2a_task(tid: str):
        t = svc.store.get_task(tid)
        if not t:
            raise HTTPException(404, "unknown task")
        return {**svc.task_row(t), "task": svc.snapshot(t, "1.0", None), "status": t["state"], "jobs": t["jobs"], "audit": svc.store.audit_rows(t["client_id"], 20)}
