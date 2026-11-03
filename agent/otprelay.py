"""OTP relay: a cloud phone's task needs a one-time code that the service texts to the owner's REAL phone.

When a task on a cloud phone asks for a code (the worker's `request_otp`), the owner's connected phone is asked to watch its notifications for a short window.
A code it finds is held in memory here, shown on the card only as a masked hint, and typed on the cloud phone only after the owner approves THAT code with one
click. Nothing else changes: the owner can always type the digits instead.

The code is never logged, stored in the database, put in a message, a job result, a hub event or an API answer, or shown to a model. It lives in `_cands` (memory)
until it is used, replaced, expired or the card is gone. See docs/otp-relay.md.
"""
import logging
import re
import threading
import time
from collections import deque

from agent import logs

log = logging.getLogger("agent.otp")

WINDOW_S = 120  # how long the phone watches
EXTEND_TO_S = 180  # one extension while the owner is still waiting
POLL_S = 2.0
CANDIDATE_TTL_S = 180  # a found code is usable for this long
MAX_WATCHES_PER_HOUR = 3  # per phone
CODE_RE = re.compile(r"\d{4,8}")
DIGITS_RE = re.compile(r"\d{4,}")

REASONS = {
    "off": "Fetching codes is switched off in the app on {phone}",
    "no_accessibility": "Turn on Mobile Agent in Accessibility on {phone}",
    "notifications_blocked": "Your phone hid the message text. Open Messages on it, or type the code here",
    "redacted": "Your phone hid the message text. Open Messages on it, or type the code here",
    "offline": "Your phone is not connected",
    "timeout": "No code arrived in 2 minutes. Ask for a new one or type it here",
    "expired": "The code expired. Ask for a new one or type it here",
    "unavailable": "This version of the app cannot fetch codes. Update the app on {phone}",
    "limit": "Too many code requests in the last hour. Type the code here",
    "none": "Connect your phone to fetch codes automatically",
}


def mask(code: str) -> str:
    """Bullets and only the last two digits: enough to tell two codes apart, useless to anyone reading over a shoulder."""
    return "•" * (len(code) - 2) + code[-2:]


def clean_label(text, limit: int = 40) -> str:
    """A sender or app name from the phone, with anything that looks like a code removed (a title can quote the code itself)."""
    return DIGITS_RE.sub("…", logs.redact(str(text or "")).strip())[:limit]


class OtpRelay:
    def __init__(self, db, hub, jobs, clock=time.time, window_s: float = WINDOW_S, extend_s: float = EXTEND_TO_S, poll_s: float = POLL_S,
                 ttl_s: float = CANDIDATE_TTL_S, max_watches: int = MAX_WATCHES_PER_HOUR):
        self.db, self.hub, self.jobs, self.clock = db, hub, jobs, clock
        self.window_s, self.extend_s, self.poll_s, self.ttl_s, self.max_watches = window_s, extend_s, poll_s, ttl_s, max_watches
        self._cands: dict[str, dict] = {}  # item id -> {code, id, phone, name, source, title, at, found} : memory only
        self._watches: dict[str, threading.Event] = {}  # item id -> stop signal (one watch per item)
        self._starts: dict[str, deque] = {}  # phone id -> times a watch was started (rate limit)
        self._lock = threading.Lock()

    @property
    def phones(self):
        return getattr(self.jobs, "phones", None)

    # ---- choosing the owner's phone
    def pick(self, jr) -> tuple[str, str] | None:
        """(phone id, its bot's name): a connected real phone other than the one the job runs on, the most recently connected first."""
        hub = self.phones
        if hub is None:
            return None
        here = jr.target.split(":", 1)[1] if (jr.target or "").startswith("phone:") else None
        links = getattr(hub, "links", {}) or {}
        found = []
        for b in self.db.list_bots():
            pid = b.get("phone_id")
            if b.get("kind") == "phone" and pid and pid != here and hub.online(pid):
                found.append((getattr(links.get(pid), "since", 0), pid, b["name"]))
        if not found:
            return None
        _, pid, name = max(found)
        return pid, name

    # ---- starting
    def begin(self, item: dict, jr) -> None:
        """Called right after the worker's code card exists. Never raises: the relay is an extra, the card works without it."""
        try:
            if str(jr.target or "").startswith("phone:") and self.pick(jr) is None:
                return  # the task runs on the owner's own phone: its screen receives the SMS itself, nothing to relay
            chosen = self.pick(jr)
            if chosen is None:
                self._relay(item["id"], "none", phone="")
                return
            pid, name = chosen
            with self._lock:
                if item["id"] in self._watches:
                    return
                recent = self._starts.setdefault(pid, deque())
                while recent and self.clock() - recent[0] > 3600:
                    recent.popleft()
                if len(recent) >= self.max_watches:
                    over = True
                else:
                    over = False
                    recent.append(self.clock())
                    self._watches[item["id"]] = threading.Event()
            if over:
                self._relay(item["id"], "unavailable", phone=name, reason=REASONS["limit"])
                return
            logs.audit("otp relay started", phone=name, item=item["id"])
            threading.Thread(target=self._watch, args=(item["id"], jr, pid, name), daemon=True, name="otp-relay").start()
        except Exception:
            log.warning("could not start the otp relay", exc_info=True)

    # ---- the card's relay line (never carries a code)
    def _relay(self, item_id: str, state: str, phone: str = "", hint: str = "", source: str = "", title: str = "", found: float | None = None, reason: str | None = None) -> None:
        item = self.db.get_item(item_id)
        if not item or not item.get("message_id"):
            return
        m = self.db.get_message(item["message_id"])
        if not m:
            return
        relay = {"state": state, "phone": phone, "hint": hint, "source": source, "title": title,
                 "age_s": int(max(0, self.clock() - found)) if found else 0, "at": found or 0,
                 "reason": REASONS.get(state, "").format(phone=phone or "your phone") if reason is None else reason}
        if m["meta"].get("relay") == relay:
            return
        upd = self.db.update_message(m["id"], meta={**m["meta"], "relay": relay})
        self.hub.publish(item["bot_id"], {"type": "message", "message": upd})

    def _open(self, item_id: str, jr) -> bool:
        item = self.db.get_item(item_id)
        return bool(item and item["status"] == "open") and not jr.cancel.is_set() and not jr.timed_out

    # ---- the watch
    def _call(self, pid: str, op: str, args: dict | None = None):
        return self.phones.call(pid, op, args or {}, timeout=10, grace=3)

    def _watch(self, item_id: str, jr, pid: str, name: str) -> None:
        stop = self._watches.get(item_id)
        end_state = None
        try:
            try:
                r = self._call(pid, "otp_watch", {"seconds": int(self.window_s)})
            except RuntimeError as e:  # an app that does not know the op answers 'refused'
                end_state = "unavailable" if "refus" in str(e).lower() or "unknown" in str(e).lower() else "offline"
                return
            except (ConnectionError, TimeoutError):
                end_state = "offline"
                return
            if not (r or {}).get("enabled", False):
                reason = (r or {}).get("reason") or "off"
                end_state = reason if reason in REASONS else "off"
                return
            self._relay(item_id, "watching", phone=name)
            t0, deadline, extended = self.clock(), self.clock() + self.window_s, False
            redacted = False
            while self._open(item_id, jr) and not stop.is_set():
                now = self.clock()
                if now >= deadline:
                    if not extended and self.extend_s > self.window_s and item_id not in self._cands:
                        extended, deadline = True, t0 + self.extend_s
                        try:
                            self._call(pid, "otp_watch", {"seconds": int(self.extend_s - self.window_s)})
                        except Exception:
                            pass
                        continue
                    break
                try:
                    r = self._call(pid, "otp_poll") or {}
                except (ConnectionError, TimeoutError, RuntimeError):
                    end_state = "offline"
                    return
                codes = [c for c in (r.get("codes") or []) if isinstance(c, dict) and isinstance(c.get("code"), str) and CODE_RE.fullmatch(c["code"])]
                if codes:
                    c = max(codes, key=lambda x: x.get("at_ms") or 0)
                    with self._lock:
                        have = self._cands.get(item_id)
                        new = have is None or have["id"] != str(c.get("id"))
                        if new:
                            self._cands[item_id] = {"code": c["code"], "id": str(c.get("id")), "phone": pid, "name": name, "source": clean_label(c.get("source")),
                                                    "title": clean_label(c.get("title")), "found": self.clock(), "expires": self.clock() + self.ttl_s}
                    if new:
                        for x in codes:  # the phone forgets what we have taken
                            try:
                                self._call(pid, "otp_ack", {"id": str(x.get("id"))})
                            except Exception:
                                pass
                        cand = self._cands[item_id]
                        self._relay(item_id, "found", phone=name, hint=mask(cand["code"]), source=cand["source"], title=cand["title"], found=cand["found"])
                elif r.get("redacted") and not redacted and item_id not in self._cands:
                    redacted = True
                    self._relay(item_id, "redacted", phone=name)
                elif r.get("watching") is False and item_id not in self._cands:
                    break
                self._pause(stop)
            # the window is over: a code that was found stays usable until its own expiry or the card closes
            while self._open(item_id, jr) and not stop.is_set() and item_id in self._cands:
                if self.clock() >= self._cands[item_id]["expires"]:
                    end_state = "expired"
                    break
                self._pause(stop)
            else:
                if self._open(item_id, jr) and not stop.is_set():
                    end_state = "timeout"
        finally:
            self._end(item_id, pid, name, end_state, jr)

    def _pause(self, stop: threading.Event) -> None:
        stop.wait(self.poll_s)

    def _end(self, item_id: str, pid: str, name: str, state: str | None, jr) -> None:
        with self._lock:
            self._cands.pop(item_id, None)
            self._watches.pop(item_id, None)
        item = self.db.get_item(item_id)
        if state and item and item["status"] == "open":
            self._relay(item_id, "expired" if state in ("timeout", "expired") else state, phone=name, reason=REASONS[state].format(phone=name))
        try:
            self.phones.call(pid, "otp_stop", {}, timeout=5, grace=0)  # best effort: the phone wipes its memory of codes
        except Exception:
            pass
        logs.audit("otp relay ended: " + (state or "answered or closed"), item=item_id)

    # ---- the owner approves
    def take(self, item_id: str) -> tuple[str, str] | None:
        """(code, phone name) of the candidate for this card, once: it is removed here. None if there is none or it is older than its lifetime."""
        with self._lock:
            c = self._cands.pop(item_id, None)
        if not c or self.clock() > c["expires"]:
            return None
        logs.audit("otp code approved", phone=c["name"], item=item_id)
        return c["code"], c["name"]

    def finish(self, item_id: str) -> None:
        """The card was answered (typed, approved or declined): stop watching, forget everything, and clear the masked hint from the card."""
        with self._lock:
            self._cands.pop(item_id, None)
            stop = self._watches.get(item_id)
        if stop:
            stop.set()
        item = self.db.get_item(item_id)
        if item and item.get("message_id"):
            m = self.db.get_message(item["message_id"])
            if m and m["meta"].get("relay"):
                meta = {k: v for k, v in m["meta"].items() if k != "relay"}
                self.hub.publish(item["bot_id"], {"type": "message", "message": self.db.update_message(m["id"], meta=meta)})

    def has_candidate(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._cands
