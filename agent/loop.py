"""Building blocks for the agent graph: prompt, screenshot shrinking, action parsing and execution."""
import hashlib
import io
import json
import re
import time

from PIL import Image

SHOT_WIDTH = 540

SYSTEM = """You operate an Android phone to accomplish the user's goal, one action per turn.
Each turn you receive a screenshot (WIDTHxHEIGHT pixels), a list of on-screen UI elements with ids and
centre coordinates in screenshot pixels, and a short history of your previous actions.

Reply with ONLY one JSON object: {"thought": "<one short sentence>", "action": {...}} where action is one of:
  {"type":"tap","element":<id>}            tap a listed element (preferred)
  {"type":"tap","x":<int>,"y":<int>}       tap screenshot coordinates when no element fits
  {"type":"type","text":"..."}             type into the focused field (never passwords or codes)
  {"type":"swipe","direction":"up|down|left|right"}   scroll/swipe the screen content
  {"type":"key","key":"back|home|enter"}
  {"type":"open_app","package":"com.example.app"}
  {"type":"open_url","url":"https://..."}
  {"type":"wait"}                          screen still loading
  {"type":"ask","question":"..."}          stop and ask the owner before anything consequential
  {"type":"handoff","reason":"..."}        the owner must act on the phone (sign in, password, captcha, 2FA prompt)
  {"type":"request_otp","app":"..."}       a one-time SMS code is needed; the owner's phone supplies it, focus the code field first
  {"type":"need_info","field":"...","why":"..."}   the screen needs a plain value you do not have (a city, a name, a date); never for passwords or codes
  {"type":"done","answer":"..."}           goal achieved; answer holds any information requested
  {"type":"fail","reason":"..."}           goal cannot be achieved

Rules: verify on the screen that the goal is met before "done". Never send messages, pay, delete, purchase, or
change account/security settings without first using "ask". Never type a password or a verification code yourself:
use "handoff" for passwords and "request_otp" for SMS codes. If the screen did not change after an action, try a
different approach instead of repeating it. Prefer element ids over coordinates. If a value you need is not
visible, the row that names it (for example "Android version") often opens a detail screen: tap it instead of
scrolling back and forth."""

KEYS = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER"}
GATES = {"ask", "handoff", "request_otp", "need_info"}


def shrink(png: bytes) -> tuple[bytes, float]:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    scale = img.width / SHOT_WIDTH
    img = img.resize((SHOT_WIDTH, round(img.height / scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), scale


def oscillating(acts: list[dict]) -> bool:
    """True when the last four actions are swipes that alternate direction (a, b, a, b)."""
    last = acts[-4:]
    if len(last) < 4 or any(a["type"] != "swipe" for a in last):
        return False
    d = [a["direction"] for a in last]
    return d[0] == d[2] and d[1] == d[3] and d[0] != d[1]


def parse_action(text: str) -> dict:
    """Extract {"thought","action"} from model text; raises ValueError when invalid."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    obj = json.loads(m.group(0))
    act = obj.get("action")
    if not isinstance(act, dict) or "type" not in act:
        raise ValueError("missing action.type")
    return obj


def build_prompt(goal: str, history: list[str], elements, scale: float, unchanged: int, acts: list[dict]) -> str:
    listing = "\n".join(f"{e.id}: {e.label!r} at ({round(e.center[0] / scale)},{round(e.center[1] / scale)})"
                        f"{' [clickable]' if e.clickable else ''}" for e in elements) or "(none)"
    hint = f"\nWARNING: the screen has not changed for {unchanged} action(s); change approach." if unchanged else ""
    if oscillating(acts):
        hint += ("\nWARNING: you keep scrolling up and down without progress. Stop scrolling; tap a row that "
                 "could open the detail you need, or use the search field.")
    return (f"Goal: {goal}\n\nRecent actions:\n" + ("\n".join(history[-6:]) or "(none)")
            + f"\n\nUI elements:\n{listing}{hint}\n\nChoose the next action.")


def execute(device, act: dict, elements, scale: float, size: tuple[int, int]) -> None:
    kind = act["type"]
    w, h = size
    if kind == "tap":
        if "element" in act:
            e = next((e for e in elements if e.id == int(act["element"])), None)
            if e is None:
                raise ValueError(f"no element {act['element']}")
            device.tap(*e.center)
        else:
            device.tap(round(float(act["x"]) * scale), round(float(act["y"]) * scale))
    elif kind == "type":
        device.type_text(str(act["text"]))
    elif kind == "swipe":
        cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
        d = {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
             "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}[act["direction"]]
        device.swipe(*d)
    elif kind == "key":
        device.key(KEYS[act["key"]])
    elif kind == "open_app":
        device.open_app(str(act["package"]))
    elif kind == "open_url":
        device.open_url(str(act["url"]))
    elif kind == "wait":
        time.sleep(1.5)
    else:
        raise ValueError(f"unknown action type {kind!r}")


def settle(device, pause: float, timeout: float = 4.0) -> None:
    """Wait until two consecutive screenshots match, so we judge the screen after the transition."""
    time.sleep(pause)
    deadline, prev = time.time() + timeout, None
    while time.time() < deadline:
        cur = hashlib.sha1(device.screenshot()).hexdigest()
        if cur == prev:
            return
        prev = cur
        time.sleep(0.4)
