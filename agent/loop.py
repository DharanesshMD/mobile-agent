"""Observe -> plan -> act -> verify loop that lets a Meta model operate an Android device."""
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

SHOT_WIDTH = 540

SYSTEM = """You operate an Android phone to accomplish the user's goal, one action per turn.
Each turn you receive a screenshot (WIDTHxHEIGHT pixels), a list of on-screen UI elements with ids and
centre coordinates in screenshot pixels, and a short history of your previous actions.

Reply with ONLY one JSON object: {"thought": "<one short sentence>", "action": {...}} where action is one of:
  {"type":"tap","element":<id>}            tap a listed element (preferred)
  {"type":"tap","x":<int>,"y":<int>}       tap screenshot coordinates when no element fits
  {"type":"type","text":"..."}             type into the focused field
  {"type":"swipe","direction":"up|down|left|right"}   scroll/swipe the screen content
  {"type":"key","key":"back|home|enter"}
  {"type":"open_app","package":"com.example.app"}
  {"type":"open_url","url":"https://..."}
  {"type":"wait"}                          screen still loading
  {"type":"ask","question":"..."}          stop and ask the owner before anything consequential
  {"type":"done","answer":"..."}           goal achieved; answer holds any information requested
  {"type":"fail","reason":"..."}           goal cannot be achieved

Rules: verify on the screen that the goal is met before "done". Never send messages, pay, delete, purchase, or
change account/security settings without first using "ask". If the screen did not change after an action, try a
different approach instead of repeating it. Prefer element ids over coordinates. If a value you need is not
visible, the row that names it (for example "Android version") often opens a detail screen: tap it instead of
scrolling back and forth."""

KEYS = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER"}


@dataclass
class Result:
    status: str  # done | failed | needs_approval | max_steps
    answer: str = ""
    steps: int = 0
    history: list[str] = field(default_factory=list)


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


def run(goal: str, device, model, max_steps: int = 15, trace_dir: str | Path | None = None,
        on_step: Callable[[int, dict, str], None] | None = None, settle: float = 0.6) -> Result:
    trace = Path(trace_dir) if trace_dir else None
    if trace:
        trace.mkdir(parents=True, exist_ok=True)
    w, h = device.size()
    system = SYSTEM.replace("WIDTHxHEIGHT", f"{SHOT_WIDTH}x{round(h * SHOT_WIDTH / w)}")
    history: list[str] = []
    acts: list[dict] = []
    last_hash, unchanged = None, 0

    for step in range(1, max_steps + 1):
        png = device.screenshot()
        digest = hashlib.sha1(png).hexdigest()
        unchanged = unchanged + 1 if digest == last_hash else 0
        last_hash = digest
        small, scale = shrink(png)
        elements = device.elements()
        listing = "\n".join(f"{e.id}: {e.label!r} at ({round(e.center[0] / scale)},{round(e.center[1] / scale)})"
                            f"{' [clickable]' if e.clickable else ''}" for e in elements) or "(none)"
        hint = (f"\nWARNING: the screen has not changed for {unchanged} action(s); change approach."
                if unchanged else "")
        if oscillating(acts):
            hint += ("\nWARNING: you keep scrolling up and down without progress. Stop scrolling; tap a row that "
                     "could open the detail you need, or use the search field.")
        prompt = (f"Goal: {goal}\n\nRecent actions:\n" + ("\n".join(history[-6:]) or "(none)")
                  + f"\n\nUI elements:\n{listing}{hint}\n\nChoose the next action.")

        obj = None
        for attempt in range(2):
            reply = model.complete(system, prompt, small)
            try:
                obj = parse_action(reply)
                break
            except (ValueError, json.JSONDecodeError):
                prompt += "\n\nYour last reply was not valid JSON. Reply with only the JSON object."
        if obj is None:
            return Result("failed", "model returned invalid JSON twice", step, history)

        act, thought = obj["action"], str(obj.get("thought", ""))
        if trace:
            (trace / f"step{step:02d}.png").write_bytes(small)
            (trace / f"step{step:02d}.json").write_text(json.dumps({"prompt": prompt, "reply": obj}, indent=2))
        if on_step:
            on_step(step, act, thought)

        kind = act["type"]
        acts.append(act)
        if kind == "done":
            return Result("done", str(act.get("answer", "")), step, history)
        if kind == "fail":
            return Result("failed", str(act.get("reason", "")), step, history)
        if kind == "ask":
            return Result("needs_approval", str(act.get("question", "")), step, history)

        try:
            execute(device, act, elements, scale, (w, h))
            outcome = "ok"
        except Exception as e:  # bad element id, adb error: tell the model and carry on
            outcome = f"error: {e}"
        history.append(f"{step}. {json.dumps(act)} -> {outcome}")
        _settle(device, settle)

    return Result("max_steps", "step budget exhausted", max_steps, history)


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


def _settle(device, pause: float, timeout: float = 4.0) -> None:
    """Wait until two consecutive screenshots match, so we judge the screen after the transition."""
    time.sleep(pause)
    deadline, prev = time.time() + timeout, None
    while time.time() < deadline:
        cur = hashlib.sha1(device.screenshot()).hexdigest()
        if cur == prev:
            return
        prev = cur
        time.sleep(0.4)
