"""Building blocks for the agent graph: prompt, screenshot shrinking, action parsing and execution."""
import io
import json
import re
import time

from PIL import Image, ImageChops, ImageStat

from agent.apps import SETTINGS_PAGES, build_intent, match_apps

SHOT_WIDTH = 540

SYSTEM = """You operate an Android phone to accomplish the user's goal, one action per turn.
Each turn you receive a list of on-screen UI elements with ids, centre coordinates in screenshot pixels (WIDTHxHEIGHT) and state
(for example [off], [on], [scrollable], [text field]), a short history of your previous actions, and usually a screenshot. When no
screenshot is attached, rely on the element list and use element ids, not coordinates.

Reply with ONLY one JSON object: {"thought": "<one short sentence>", "action": {...}} where action is one of:
  {"type":"tap","element":<id>}            tap a listed element (preferred)
  {"type":"tap","x":<int>,"y":<int>}       tap screenshot coordinates when no element fits
  {"type":"type","text":"..."}             type into the focused field (never passwords or codes)
  {"type":"scroll","direction":"down|up|left|right"}  reveal more content in that direction: "down" shows what is further down the page
  {"type":"swipe","direction":"up|down|left|right"}   raw finger swipe (the finger moves in that direction); prefer "scroll"
  {"type":"find_app","query":"calculator"} list installed apps matching a name; use the package it returns with open_app
  {"type":"intent","kind":"set_alarm|set_timer|dial|compose_sms|compose_email|insert_contact|insert_event|maps_search|navigate|share_text|camera", ...}
                                           do it without tapping through the app. Fields by kind: set_alarm hour (0-23), minute, label?; set_timer seconds, label?;
                                           dial number; compose_sms number, body; compose_email to, subject, body; insert_contact name, phone, email?;
                                           insert_event title, start "YYYY-MM-DD HH:MM", minutes?, location?; maps_search query; navigate destination; share_text text
  {"type":"open_settings","page":"wifi|bluetooth|display|sound|battery|apps|storage|location|security|privacy|accounts|add_account|date_time|language|keyboard|accessibility|airplane_mode|nfc|network|data_usage|notifications|do_not_disturb|about_phone|developer_options|default_apps"}
                                           jump straight to a Settings page (much faster than hunting through menus)
  {"type":"key","key":"back|home|enter"}
  {"type":"open_app","package":"com.example.app"}
  {"type":"open_url","url":"https://..."}
  {"type":"wait"}                          screen still loading
  {"type":"ask","question":"..."}          stop and ask the owner before anything consequential
  {"type":"handoff","reason":"..."}        the owner must act on the phone (sign in, password, captcha, 2FA prompt)
  {"type":"type_secret","label":"...","app":"..."}  type a saved secret (password, card or ID number) into the focused field; the owner approves each use and you never see the value. Only labels listed under "Saved secrets" exist
  {"type":"request_otp","app":"..."}       a one-time SMS code is needed; the owner's phone supplies it, focus the code field first
  {"type":"need_info","field":"...","why":"..."}   the screen needs a plain value you do not have (a city, a name, a date); never for passwords or codes
  {"type":"done","answer":"..."}           goal achieved; answer holds any information requested
  {"type":"fail","reason":"..."}           goal cannot be achieved

Rules: verify on the screen that the goal is met before "done". Never send messages, pay, delete, purchase, or
change account/security settings without first using "ask". Never type a password or a verification code yourself:
use "type_secret" for a saved secret, otherwise "handoff" for passwords and "request_otp" for SMS codes. Prefer "intent" over tapping when a kind fits. set_alarm and set_timer finish by themselves (the result confirms them). compose_sms, compose_email,
insert_contact and insert_event only PREFILL a screen: you still tap Send or Save, and you must "ask" the owner before anything is sent. dial never places the call.
To open an app, call find_app first, then open_app with a returned package. If find_app says the app is not installed, do not hunt for it: use "ask"
to offer installing it from the Play Store when signed in, otherwise "fail". For anything in Settings, use open_settings first, then tap the
exact toggle or row. If the screen did not change after an action, try a
different approach instead of repeating it. Prefer element ids over coordinates. If a value you need is not
visible, the row that names it (for example "Android version") often opens a detail screen: tap it instead of
scrolling back and forth."""

KEYS = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER"}
GATES = {"ask", "handoff", "request_otp", "need_info", "type_secret"}


def shrink(png: bytes, width: int = SHOT_WIDTH) -> tuple[bytes, float]:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    scale = img.width / width
    img = img.resize((width, round(img.height / scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), scale


def oscillating(acts: list[dict]) -> bool:
    """True when the last four actions are swipes that alternate direction (a, b, a, b)."""
    last = acts[-4:]
    if len(last) < 4 or any(a["type"] not in ("swipe", "scroll") for a in last):
        return False
    d = [a["direction"] for a in last]
    return d[0] == d[2] and d[1] == d[3] and d[0] != d[1]


def need_image(mode: str, step: int, elements, unchanged: int, acts: list[dict], history: list[str]) -> bool:
    """Whether to attach the screenshot this turn. Adaptive: look when the text view is likely not enough."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return (step <= 1 or len(elements) < 5 or unchanged > 0 or oscillating(acts)
            or (bool(history) and "-> error" in history[-1]) or (bool(acts) and acts[-1]["type"] == "wait"))


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


def build_prompt(goal: str, history: list[str], elements, scale: float, unchanged: int, acts: list[dict],
                 keyboard: bool = False, secrets: list[str] | None = None) -> str:
    listing = "\n".join(f"{e.id}: {e.label!r} at ({round(e.center[0] / scale)},{round(e.center[1] / scale)})"
                        f"{' [clickable]' if e.clickable else ''}{f' [{e.state}]' if getattr(e, 'state', '') else ''}"
                        for e in elements) or "(UI element list unavailable right now: use screenshot coordinates)"
    hint = f"\nWARNING: the screen has not changed for {unchanged} action(s); change approach." if unchanged else ""
    if oscillating(acts):
        hint += ("\nWARNING: you keep scrolling up and down without progress. Stop scrolling; tap a row that "
                 "could open the detail you need, or use the search field.")
    return (f"Goal: {goal}\n\nRecent actions:\n" + ("\n".join(history[-6:]) or "(none)")
            + ("\n\nOn-screen keyboard: open (its keys are not listed; use type to enter text, key back to close it)." if keyboard else "")
            + ("\n\nSaved secrets (labels only): " + ", ".join(secrets) if secrets else "")
            + f"\n\nUI elements:\n{listing}{hint}\n\nChoose the next action.")


def execute(device, act: dict, elements, scale: float, size: tuple[int, int]) -> str | None:
    """Run one action. Returns text to show the model (for lookups), or None."""
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
    elif kind in ("swipe", "scroll"):
        cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
        if getattr(device, "keyboard_open", False):
            # A swipe that starts over the keyboard is read as glide typing and inserts random words, so stay in the content above it.
            cy, dy = round(h * 0.30), round(h * 0.18)
        direction = act["direction"]
        if kind == "scroll":  # "scroll down" reveals lower content, so the finger moves up
            direction = {"down": "up", "up": "down", "left": "right", "right": "left"}[direction]
        d = {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
             "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}[direction]
        device.swipe(*d)
    elif kind == "find_app":
        found = match_apps(str(act.get("query", "")), device.launcher_apps())
        return ("installed: " + ", ".join(found)) if found else f"no installed app matches '{act.get('query', '')}' (it is not installed)"
    elif kind == "intent":
        args, note = build_intent(str(act.get("kind", "")), act, getattr(device, "timezone", lambda: "UTC")())
        device.start_intent(args)
        if act["kind"] == "set_alarm" and hasattr(device, "next_alarm"):
            note += f"; next alarm on the phone: {device.next_alarm() or 'none found'}"
        if act["kind"] == "set_timer" and hasattr(device, "has_timer"):
            note += "; a timer is running" if device.has_timer() else "; could not confirm the timer"
        return note
    elif kind == "open_settings":
        page = str(act.get("page", "")).lower().replace(" ", "_").replace("-", "_")
        if page not in SETTINGS_PAGES:
            raise ValueError(f"unknown settings page '{page}'; use one of: {', '.join(SETTINGS_PAGES)}")
        device.open_intent(SETTINGS_PAGES[page])
        return f"opened {page} settings"
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


def _signature(png: bytes) -> Image.Image:
    """Small grayscale view of the screen without the status bar and gesture bar (clock, battery, cursor blinks)."""
    img = Image.open(io.BytesIO(png)).convert("L")
    w, h = img.size
    return img.crop((0, int(h * 0.06), w, int(h * 0.94))).resize((48, 106))


def settle(device, pause: float, timeout: float = 4.0, tolerance: float = 1.5) -> None:
    """Wait until two consecutive screens match closely, so we judge the screen after the transition."""
    time.sleep(pause)
    deadline, prev = time.time() + timeout, None
    while time.time() < deadline:
        cur = _signature(device.screenshot())
        if prev is not None and ImageStat.Stat(ImageChops.difference(prev, cur)).mean[0] <= tolerance:
            return
        prev = cur
        time.sleep(0.15)
