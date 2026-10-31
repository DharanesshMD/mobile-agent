"""A real Android phone as a device: the Mobile Agent app on the phone keeps one WebSocket open to the server and answers requests.

The server sends {"id": n, "op": "tap", "args": {...}}; the phone answers {"id": n, "ok": true, "result": ...} or {"id": n, "ok": false, "error": "..."}.
The phone dials out, so it works behind any router or firewall. `RemoteDevice` has the same methods as `agent.device.Device`, so the worker,
the checks and the queue run unchanged on it.
"""
import asyncio
import base64
import io
import threading
import time
from typing import Any

from PIL import Image

from agent.device import Element

CALL_TIMEOUT = 40  # a screenshot or a UI tree on a busy phone can take a few seconds
RECONNECT_GRACE_S = 20  # a phone that just dropped usually reconnects within seconds (Wi-Fi to mobile data, a sleeping radio)
SILENCE_S = 90  # the app sends a ping every 25 s; no sign of life for this long and the link is dropped


class PhoneOffline(ConnectionError):
    """The phone is not connected (the app is closed, or the phone has no network)."""


class Link:
    """One connected phone."""
    def __init__(self, ws, phone_id: str, info: dict):
        self.ws, self.id, self.info = ws, phone_id, info
        self.pending: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.since = time.time()

    async def request(self, op: str, args: dict) -> Any:
        self.counter += 1
        n = self.counter
        fut = asyncio.get_running_loop().create_future()
        self.pending[n] = fut
        try:
            await self.ws.send_json({"id": n, "op": op, "args": args})
            return await fut
        finally:
            self.pending.pop(n, None)

    def resolve(self, msg: dict) -> None:
        fut = self.pending.get(msg.get("id"))
        if fut and not fut.done():
            fut.set_result(msg["result"]) if msg.get("ok") else fut.set_exception(RuntimeError(str(msg.get("error", "the phone refused"))))

    def drop(self) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(PhoneOffline("the phone went offline"))


class PhoneHub:
    """Which phones are connected, and a thread-safe way to ask one of them to do something."""
    def __init__(self):
        self.links: dict[str, Link] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.on_change = lambda: None  # the app refreshes the Mobile panel when a phone connects or leaves
        self.on_connect = lambda phone_id, info: None  # the app gives a new phone its own bot

    def online(self, phone_id: str | None = None) -> bool:
        return phone_id in self.links if phone_id else bool(self.links)

    def pick(self, phone_id: str | None = None) -> str | None:
        """The phone to use: the one asked for, else the only one (or the one that connected last)."""
        if phone_id:
            return phone_id if phone_id in self.links else None
        return max(self.links.values(), key=lambda link: link.since).id if self.links else None

    def listing(self) -> list[dict]:
        return [{"id": link.id, **{k: link.info.get(k) for k in ("name", "model", "sdk", "size")}, "since": link.since} for link in self.links.values()]

    async def serve(self, ws, phone_id: str) -> None:
        """Run the socket of one phone until it closes."""
        await ws.accept()
        self.loop = asyncio.get_running_loop()
        hello = await asyncio.wait_for(ws.receive_json(), 15)
        link = Link(ws, phone_id, hello if hello.get("type") == "hello" else {})
        with self._lock:
            old = self.links.get(phone_id)
            self.links[phone_id] = link
        if old:
            old.drop()  # the same phone reconnected: whatever the old link was doing is lost
        try:
            self.on_connect(phone_id, link.info)
        except Exception:  # a failure to create the bot must not drop the phone
            pass
        self.on_change()
        try:
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), SILENCE_S)
                if "id" in msg:
                    link.resolve(msg)
                elif msg.get("type") == "hello":
                    link.info = msg
        except Exception:  # closed, silent for too long or malformed: the phone is gone
            pass
        finally:
            link.drop()
            with self._lock:
                if self.links.get(phone_id) is link:
                    self.links.pop(phone_id)
            self.on_change()

    def call(self, phone_id: str, op: str, args: dict | None = None, timeout: float = CALL_TIMEOUT, grace: float = RECONNECT_GRACE_S) -> Any:
        """Ask a phone to do one thing and wait for its answer. Safe to call from any worker thread."""
        end = time.time() + grace
        while phone_id not in self.links and time.time() < end:
            time.sleep(0.25)
        link = self.links.get(phone_id)
        if link is None or self.loop is None:
            raise PhoneOffline("the phone is offline: open the Mobile Agent app on it")
        fut = asyncio.run_coroutine_threadsafe(link.request(op, args or {}), self.loop)
        try:
            return fut.result(timeout)
        except TimeoutError:  # concurrent.futures.TimeoutError is TimeoutError since Python 3.11
            fut.cancel()
            raise TimeoutError(f"the phone did not answer '{op}' within {round(timeout)} s") from None


class RemoteDevice:
    """The `Device` interface, backed by a phone that is connected through the hub."""
    has_play = True  # a real phone has Google Play

    def __init__(self, hub: PhoneHub, phone_id: str):
        self.hub, self.phone_id = hub, phone_id
        self.keyboard_open = False

    def _call(self, op: str, **args) -> Any:
        return self.hub.call(self.phone_id, op, args)

    def is_emulator(self) -> bool:
        return False

    def size(self) -> tuple[int, int]:
        w, h = self._call("size")
        return int(w), int(h)

    def orientation(self) -> str:
        return "landscape" if self.size()[0] > self.size()[1] else "portrait"

    def screenshot(self) -> bytes:
        raw = base64.b64decode(self._call("screenshot"))
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return raw
        out = io.BytesIO()  # the phone may send JPEG to save bandwidth
        Image.open(io.BytesIO(raw)).convert("RGB").save(out, "PNG")
        return out.getvalue()

    def frame(self):
        return Image.open(io.BytesIO(self.screenshot()))

    def elements(self, limit: int = 70) -> list[Element]:
        res = self._call("elements", limit=limit)
        self.keyboard_open = bool(res.get("keyboard"))
        return [Element(i, str(e["label"]), (int(e["center"][0]), int(e["center"][1])), bool(e.get("clickable")), str(e.get("state", "")))
                for i, e in enumerate(res.get("elements", [])[:limit])]

    def tap(self, x: int, y: int) -> None:
        self._call("tap", x=int(x), y=int(y))

    def touch(self, kind: str, x: int, y: int) -> None:
        raise NotImplementedError("live dragging is not available on a real phone")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._call("swipe", x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2), ms=int(ms))

    def type_text(self, text: str) -> None:
        self._call("type", text=text)

    def key(self, name: str) -> None:
        self._call("key", name=name)

    def open_app(self, package: str) -> None:
        self._call("open_app", package=package)

    def open_url(self, url: str) -> None:
        self._call("open_url", url=url)

    def open_intent(self, action: str) -> None:
        self._call("open_intent", action=action)

    def start_intent(self, args: list[str]) -> None:
        raise ValueError("this action is not available on your own phone: use the app screens instead")

    def launcher_apps(self) -> list[str]:
        return list(self._call("launcher_apps"))

    def packages(self) -> list[str]:
        return list(self._call("packages"))

    def foreground(self) -> str:
        return str(self._call("foreground"))

    def crashes(self) -> list[tuple[str, str]]:
        return [(str(p), str(i)) for p, i in self._call("crashes")]

    def reconnect(self) -> None:
        pass
