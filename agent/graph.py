"""LangGraph orchestration: observe -> plan -> (act | gate | finish), with owner pause points.

Gates (ask, handoff, request_otp) call interrupt(), so a run can wait for the owner for any length of time
and resume exactly where it stopped. Add more agents later as extra nodes or sub-graphs on the same state.
"""
import hashlib
import json
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent import cloudtools, gamepolicy
from agent.device import Element
from agent.config import IMAGE_MODE, IMAGE_WIDTH
from agent.config import CRASH_LIMIT, MAX_JOB_MINUTES, MAX_WAIT_S
from agent.loop import ANIMATED_AFTER, ANIMATED_SAME, GATES, build_prompt, coarse, coarse_diff, execute, need_image, parse_action, shrink, system_prompt
from agent.loop import settle as settle_screen


MAX_BLOCKS = 3  # input refused by the game guard this many times in one task: the task ends instead of looping
CRASH_PREFIX = "App keeps crashing: "  # the orchestrator recognises a failed task by this start and does not retry it


def crash_report(pkg: str, n: int, emulator: bool) -> str:
    why = (" It closes itself right after it starts, which some apps and games do on an emulated phone (they detect it and refuse to run)."
           if emulator else "")
    return f"{CRASH_PREFIX}{pkg} crashed {n} times while opening, so I stopped instead of trying again and again.{why} It cannot be fixed by tapping."


class State(TypedDict, total=False):
    goal: str
    step: int
    history: list[str]
    acts: list[dict]
    last_hash: str
    last_sig: bytes  # a coarse picture of the last screen, to see that an animated game screen did not really change
    unchanged: int
    obs: dict
    action: dict
    thought: str
    status: str
    answer: str
    waits: int  # steps spent only waiting (a download, a loading screen): they do not use the step budget
    crash_seen: list[str]  # "package:pid" of crashes already counted, so each is counted once
    crashes: dict  # package -> crashes during this task
    fg: str  # package of the app in front when the screen was last read (the game guard looks at it)
    guard: str  # why input is refused on the current screen ("" when it is allowed): a blocked game, or a browser on a cloud-gaming page
    guard_name: str  # the game the guard is protecting
    site: str  # a third-party site the browser is on: nothing is typed there
    blocked: int  # inputs the game guard refused in this task
    launchers: list  # packages with a launcher icon: only these are apps whose crash can stop a task (system services crash in the background on their own)


@dataclass
class Result:
    status: str  # done | failed | denied | cancelled | max_steps | needs_approval
    answer: str = ""
    steps: int = 0
    history: list[str] = field(default_factory=list)
    pending: dict | None = None


def _c(config: RunnableConfig) -> dict:
    return config["configurable"]


def _emit(config: RunnableConfig, event: dict) -> None:
    fn = _c(config).get("emit")
    if fn:
        fn(event)


def _timed(fn):
    t = time.time()
    out = fn()
    return out, round((time.time() - t) * 1000)


def _log_timing(config: RunnableConfig, n: int, ms: dict) -> None:
    trace = _c(config).get("trace")
    if trace:
        Path(trace).mkdir(parents=True, exist_ok=True)
        with open(Path(trace) / "timing.jsonl", "a") as f:
            f.write(json.dumps({"step": n, **ms}) + "\n")


def observe(state: State, config: RunnableConfig) -> dict:
    c = _c(config)
    if c.get("pause"):
        c["pause"]()  # blocks while the owner holds control of the phone
    if c["cancel"].is_set():
        return {"status": "cancelled"}
    goal = state["goal"]
    for note in (c["steer"]() if c.get("steer") else []):  # the owner changed or added to the task while it runs
        goal += f"\nOwner update: {note}"
    step_now, waits = state.get("step", 0), state.get("waits", 0)
    if step_now - waits >= c["max_steps"] or step_now >= c["max_steps"] * 4:  # waiting for a download is free, up to a hard cap
        return {"status": "max_steps", "answer": "step budget exhausted"}
    if time.time() > c["deadline"]:
        return {"status": "max_steps", "answer": f"time budget exhausted ({round(c['max_minutes'])} min); the phone may still be working on it"}
    dev = c["device"]
    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:  # the screenshot, the UI tree, the crash log and the app in front are independent ADB calls
        f_shot, f_tree = ex.submit(_timed, dev.screenshot), ex.submit(_timed, dev.elements)
        f_fg = ex.submit(dev.foreground) if hasattr(dev, "foreground") else None
        f_crash = ex.submit(dev.crashes) if hasattr(dev, "crashes") else None
        (png, shot_ms), (elements, tree_ms) = f_shot.result(), f_tree.result()
        try:
            logged = f_crash.result() if f_crash else []
        except Exception:  # the crash log is a help, never a reason to stop
            logged = []
        try:
            fg = (f_fg.result() if f_fg else "") or state.get("fg", "")
        except Exception:  # unknown now: keep what was last seen, so a game that was in front stays guarded
            fg = state.get("fg", "")
    seen, crashes, launchers = list(state.get("crash_seen", [])), dict(state.get("crashes", {})), state.get("launchers")
    fresh = [(pkg, pid) for pkg, pid in logged if f"{pkg}:{pid}" not in seen]
    if fresh and launchers is None:  # find out once which packages are real apps (a virtual phone's Bluetooth service crashes all the time and is not the task's app)
        try:
            launchers = sorted(dev.launcher_apps()) if hasattr(dev, "launcher_apps") else []
        except Exception:
            launchers = []
    for pkg, pid in fresh:
        seen.append(f"{pkg}:{pid}")
        if "crash_seen" in state and (not launchers or pkg in launchers):  # the first look only records what crashed before this task began
            crashes[pkg] = crashes.get(pkg, 0) + 1
    crash_state = {"crash_seen": seen, "crashes": crashes, "launchers": launchers}
    worst = max(crashes, key=crashes.get, default=None)
    if worst and crashes[worst] >= CRASH_LIMIT:
        return {**crash_state, "status": "failed", "answer": crash_report(worst, crashes[worst], bool(getattr(dev, "is_emulator", lambda: False)()))}
    w0, h0 = struct.unpack(">II", png[16:24])  # the screenshot's own size (PNG header): right even if the phone rotated mid-task
    digest = hashlib.sha1(png).hexdigest()
    sig = coarse(png)
    same = digest == state.get("last_hash")
    if not same and getattr(dev, "unsettled", 0) >= ANIMATED_AFTER and state.get("last_sig"):
        same = coarse_diff(sig, state["last_sig"]) <= ANIMATED_SAME  # a game animates all the time, so no two frames are identical: judge the picture, not the bytes
    unchanged = state.get("unchanged", 0) + 1 if same else 0
    # The image width is the portrait width; in landscape keep the same short side so the picture is not tiny.
    small, scale = shrink(png, c["image_width"] if h0 >= w0 else round(c["image_width"] * w0 / h0))
    step = state.get("step", 0) + 1
    send = need_image(c["image_mode"], step, elements, unchanged, state.get("acts", []), state.get("history", []))
    prompt = build_prompt(goal, state.get("history", []), elements, scale, unchanged, state.get("acts", []),
                          getattr(dev, "keyboard_open", False), c["secrets"].labels() if c.get("secrets") else None, landscape=w0 > h0)
    guard, guard_name = gamepolicy.blocked(fg), gamepolicy.app_name(fg) if gamepolicy.blocked(fg) else ""
    if not guard:  # a browser streaming a blocked game from a cloud-gaming site is the same account at risk
        web = gamepolicy.browser_page(fg, elements, (w0, h0), state.get("guard_name", "") if state.get("guard") and state.get("fg") == fg else "")
        if web:
            guard_name = web
            guard = gamepolicy.REASON.format(name=f"{web} (in a browser, through a cloud-gaming site)", publisher="its publisher")
    site = gamepolicy.browser_site(fg, elements, (w0, h0), state.get("site", "") if state.get("fg") == fg else "")
    if site and not guard:
        prompt += (f"\n\nPOLICY: this is {site}, a third-party site. Do NOT type anything on it (no login, no code, no search box) and do not use type_secret: "
                   "if a sign-in is needed, use handoff so the owner does it. Tapping and scrolling to read are fine.")
    if c.get("observe_only"):
        prompt += ("\n\nOBSERVE-ONLY LINK CHECK: do not type, do not tap the page, do not download anything. Read the address bar and the page, use capture for what you see, "
                   "then verdict, then done. Only the browser's own first-run or warning screens may be tapped (Use without an account, No thanks, Got it, Details, Back to safety).")
    if guard:
        prompt += (f"\n\nPOLICY: {guard_name} is open here. Its publisher bans accounts that use bots, so you must NOT tap, scroll or type in it"
                   + (" (that includes cloud-gaming pages in a browser)" if guard_name and not gamepolicy.blocked(fg) else "")
                   + ". Describe what the screen shows (launch, age or login screen, menus, the page) and finish with done. Back, home, opening another page or app and reading are allowed.")
    if crashes:
        prompt += ("\n\nANDROID REPORTS APP CRASHES during this task: " + ", ".join(f"{p} crashed {n}x" for p, n in crashes.items())
                   + ". A loading screen is NOT loading if the app keeps crashing. If a 'keeps stopping' dialog is showing, tap Close app. "
                     f"Open the app again at most once more; after {CRASH_LIMIT} crashes the task is stopped.")
    ms = {"shot": shot_ms, "tree": tree_ms, "observe": round((time.time() - t0) * 1000), "image": send}
    return {**crash_state, "fg": fg, "guard": guard, "guard_name": guard_name, "site": site, "goal": goal, "step": step, "last_hash": digest, "last_sig": sig, "unchanged": unchanged,
            "obs": {"png": small, "send_image": send, "scale": scale, "size": (w0, h0), "elements": [asdict(e) for e in elements],
                    "prompt": prompt, "ms": ms}}


def plan(state: State, config: RunnableConfig) -> dict:
    c, obs = _c(config), state["obs"]
    prompt, parsed = obs["prompt"], None
    t0 = time.time()
    tokens0 = dict(getattr(c["model"], "tokens", {}) or {})
    image = obs["png"] if obs.get("send_image", True) else None
    for _ in range(2):
        reply = c["model"].complete(c["system"], prompt, image)
        try:
            parsed = parse_action(reply)
            break
        except (ValueError, json.JSONDecodeError):
            prompt += "\n\nYour last reply was not valid JSON. Reply with only the JSON object."
    if parsed is None:
        return {"status": "failed", "answer": "model returned invalid JSON twice"}
    if image is None and parsed["action"].get("type") == "done":
        # Decided from text alone: look at the screen once before accepting "done".
        check = (obs["prompt"] + "\n\nYou are about to finish with: " + json.dumps(parsed["action"])
                 + "\nCheck the screenshot. Reply with the final JSON: keep done (correct the answer if it is wrong) "
                   "or choose another action if the goal is not actually met.")
        try:
            second = parse_action(c["model"].complete(c["system"], check, obs["png"]))
            obs["ms"]["verified"] = True
            parsed = second
        except Exception:
            pass
    act, thought, n = parsed["action"], str(parsed.get("thought", "")), state["step"]
    obs["ms"]["model"] = round((time.time() - t0) * 1000)
    tokens1 = getattr(c["model"], "tokens", {}) or {}
    for k in ("prompt", "completion", "cached"):
        obs["ms"]["t_" + k] = tokens1.get(k, 0) - tokens0.get(k, 0)
    if c.get("trace"):
        t = Path(c["trace"])
        t.mkdir(parents=True, exist_ok=True)
        (t / f"step{n:02d}.png").write_bytes(obs["png"])
        (t / f"step{n:02d}.json").write_text(json.dumps({"prompt": obs["prompt"], "reply": parsed}, indent=2))
    _emit(config, {"type": "step", "n": n, "thought": thought, "action": act})
    return {"action": act, "thought": thought, "acts": state.get("acts", []) + [act], "obs": obs}


def finish(state: State) -> dict:
    act = state["action"]
    if act["type"] == "done":
        return {"status": "done", "answer": str(act.get("answer", ""))}
    return {"status": "failed", "answer": str(act.get("reason", ""))}


def gate(state: State, config: RunnableConfig) -> dict:
    c, act, n = _c(config), state["action"], state["step"]
    kind = act["type"]
    text = str(act.get("question") or act.get("reason") or act.get("app") or "")
    payload = {"kind": kind, "text": text}
    refused = _game_guard(state, c, n) if kind == "type_secret" else None
    if refused:
        return refused
    if kind == "need_info":
        payload = {"kind": kind, "field": str(act.get("field", "")), "text": str(act.get("why", ""))}
    if kind == "need_secret":
        label, secrets = str(act.get("label", "")).strip(), c.get("secrets")
        if not label:
            return {"history": state.get("history", []) + [f"{n}. need_secret -> error: a label is required"]}
        if secrets and label.lower() in [x.lower() for x in secrets.labels()]:
            return {"history": state.get("history", []) + [f"{n}. secret '{label}' is already saved: use type_secret"]}
        payload = {"kind": kind, "label": label, "secret_kind": str(act.get("kind", "password")), "text": str(act.get("why", ""))}
    if kind == "type_secret":
        label, app, secrets = str(act.get("label", "")), str(act.get("app", "")), c.get("secrets")
        if not secrets or label.lower() not in [x.lower() for x in secrets.labels()]:
            known = ", ".join(secrets.labels()) if secrets else ""
            return {"history": state.get("history", []) + [f"{n}. type_secret -> error: no saved secret '{label}'" + (f" (saved: {known})" if known else "")]}
        payload = {"kind": kind, "label": label, "text": f"Type your saved secret '{label}'" + (f" into {app}" if app else "") + "?"}
    resume = interrupt(payload)
    if resume.get("cancelled"):
        return {"status": "cancelled"}
    history = state.get("history", [])
    if kind == "need_info":
        value = str(resume.get("value") or "").strip()
        if not value:
            return {"status": "denied", "answer": "owner did not provide " + payload["field"]}
        history = history + [f"{n}. owner provided {payload['field']}: {value}"]
    elif kind == "ask":
        if not resume.get("approved"):
            return {"status": "denied", "answer": text}
        history = history + [f"{n}. owner approved: {text}"]
    elif kind == "handoff":
        history = history + [f"{n}. owner handled on the phone: {text}"]
    elif kind == "need_secret":
        if not resume.get("saved"):
            return {"status": "denied", "answer": f"owner did not save '{label}'"}
        history = history + [f"{n}. owner saved secret '{resume['saved']}': use type_secret to enter it"]
    elif kind == "type_secret":
        if not resume.get("approved"):
            return {"status": "denied", "answer": f"owner did not allow using '{label}'"}
        c["device"].type_text(secrets.reveal(label, app))  # typed here only: never put in state, history, events or the model prompt
        history = history + [f"{n}. typed saved secret '{label}'"]
    else:
        code = str(resume.get("code") or "")
        if not code:
            return {"status": "denied", "answer": "owner did not share a code"}
        c["device"].type_text(code)  # typed here only: never put in state, history, events or the model prompt
        history = history + [f"{n}. one-time code entered"]
    settle_screen(c["device"], c["settle"])
    return {"history": history}


def _game_guard(state: State, c: dict, n: int) -> dict | None:
    """Refuse input into a game whose publisher bans bots. The model is told once, with the way out; after MAX_BLOCKS tries the task ends."""
    reason = state.get("guard")
    if not reason and state.get("site") and state["action"].get("type") in ("type", "type_secret"):  # a third-party site: the owner types logins, never the agent
        count = state.get("blocked", 0) + 1
        line = f"{n}. {json.dumps({k: v for k, v in state['action'].items() if k != 'text'})} -> error: {gamepolicy.CREDS_REASON.format(host=state['site'])} Use handoff or finish with done."
        out = {"history": state.get("history", []) + [line], "blocked": count}
        if count >= MAX_BLOCKS:
            out.update(status="denied", answer=gamepolicy.CREDS_PREFIX + f"nothing was typed on {state['site']}. Sign in yourself; I can hand the phone to you.")
        return out
    if not reason:
        return None
    if gamepolicy.browser_click_ok(state["action"], [Element(e["id"], e["label"], tuple(e["center"]), e["clickable"], e.get("state", "")) for e in state["obs"]["elements"]]):
        return None  # closing the tab or opening a new one on a cloud-gaming page: leaving is never gameplay
    count = state.get("blocked", 0) + 1
    line = f"{n}. {json.dumps(state['action'])} -> error: {reason} Do not try again: finish with done and describe what the screen shows."
    out: dict = {"history": state.get("history", []) + [line], "blocked": count}
    if count >= MAX_BLOCKS:
        out.update(status="denied", answer=gamepolicy.POLICY_PREFIX + f"{state.get('guard_name') or gamepolicy.app_name(state['fg'])} was opened but not played: its publisher bans accounts that use bots. "
                                           "To play it yourself use Take control on this phone.")
    return out


def act(state: State, config: RunnableConfig) -> dict:
    c, obs, a, n = _c(config), state["obs"], state["action"], state["step"]
    elements = [Element(e["id"], e["label"], tuple(e["center"]), e["clickable"], e.get("state", "")) for e in obs["elements"]]
    refused = _game_guard(state, c, n) if gamepolicy.is_input(a) else None  # a game, a cloud-gaming page, or typing on a third-party site
    if refused:
        _emit(config, {"type": "result", "n": n, "outcome": refused["history"][-1].split("-> ", 1)[-1]})
        return refused
    if c.get("observe_only"):  # a link check: the page is never typed into or tapped (only the browser's own first-run and warning screens)
        why = cloudtools.observe_block(a, elements)
        if why:
            count = state.get("blocked", 0) + 1
            out = {"history": state.get("history", []) + [f"{n}. {json.dumps({k: v for k, v in a.items() if k != 'text'})} -> error: {why}"], "blocked": count}
            if count >= MAX_BLOCKS:
                out.update(status="denied", answer="Observe-only link check: the agent does not type into or tap a suspicious page.")
            _emit(config, {"type": "result", "n": n, "outcome": out["history"][-1].split("-> ", 1)[-1]})
            return out
    t0 = time.time()
    try:
        if a["type"] in cloudtools.TOOLS:
            info = cloudtools.run(c, state, a)
        elif a["type"] == "wait" and a.get("seconds") is not None:
            secs = max(1, min(int(a["seconds"]), MAX_WAIT_S))
            end = time.time() + secs
            while time.time() < end and not c["cancel"].is_set():  # in small slices, so Stop works at once
                time.sleep(0.25)
            info = f"waited {secs} s"
        else:
            info = execute(c["device"], a, elements, obs["scale"], tuple(obs.get("size") or c["size"]))
        outcome = "ok" + (f": {info}" if info else "")
    except Exception as e:  # bad element id, adb error: tell the model and carry on
        outcome = f"error: {e}"
    act_ms = round((time.time() - t0) * 1000)
    _emit(config, {"type": "result", "n": n, "outcome": outcome})
    t1 = time.time()
    settle_screen(c["device"], c["settle"])
    _log_timing(config, n, {**obs.get("ms", {}), "act": act_ms, "settle": round((time.time() - t1) * 1000), "action": a["type"]})
    return {"history": state.get("history", []) + [f"{n}. {json.dumps(a)} -> {outcome}"], "waits": state.get("waits", 0) + (a["type"] == "wait")}


def _done(state: State) -> str:
    return END if state.get("status") else "observe"


def _after_observe(state: State) -> str:
    return END if state.get("status") else "plan"


def _after_plan(state: State) -> str:
    if state.get("status"):
        return END
    t = state["action"]["type"]
    return "finish" if t in ("done", "fail") else "gate" if t in GATES else "act"


def build_graph(checkpointer=None):
    g = StateGraph(State)
    for name, fn in (("observe", observe), ("plan", plan), ("act", act), ("gate", gate), ("finish", finish)):
        g.add_node(name, fn)
    g.add_edge(START, "observe")
    g.add_conditional_edges("observe", _after_observe, ["plan", END])
    g.add_conditional_edges("plan", _after_plan, ["finish", "gate", "act", END])
    g.add_conditional_edges("act", _done, ["observe", END])
    g.add_conditional_edges("gate", _done, ["observe", END])
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


def run(goal: str, device, model, max_steps: int = 15, trace_dir: str | Path | None = None,
        on_step: Callable[[int, dict, str], None] | None = None, settle: float = 0.4,
        handler: Callable[[dict], dict] | None = None, emit: Callable[[dict], None] | None = None,
        cancel: threading.Event | None = None, graph=None, pause: Callable[[], None] | None = None,
        image_mode: str | None = None, image_width: int | None = None, secrets=None,
        steer: Callable[[], list[str]] | None = None, max_minutes: float = MAX_JOB_MINUTES, artifacts=None, observe_only: bool = False) -> Result:
    """Run a goal to completion. Without a handler, the run stops at the first gate with status needs_approval."""
    w, h = device.size()
    image_mode, image_width = image_mode or IMAGE_MODE, image_width or IMAGE_WIDTH

    def fan_out(event: dict) -> None:
        if event["type"] == "step" and on_step:
            on_step(event["n"], event["action"], event["thought"])
        if emit:
            emit(event)

    config = {"recursion_limit": max_steps * 4 * 3 + 30, "configurable": {"deadline": time.time() + max_minutes * 60, "max_minutes": max_minutes,
        "thread_id": uuid.uuid4().hex, "device": device, "model": model, "size": (w, h), "settle": settle,
        "max_steps": max_steps, "trace": str(trace_dir) if trace_dir else None, "emit": fan_out,
        "cancel": cancel or threading.Event(), "pause": pause, "steer": steer, "artifacts": artifacts or cloudtools.Artifacts(), "observe_only": observe_only, "secrets": secrets, "image_mode": image_mode, "image_width": image_width,
        "system": system_prompt(getattr(device, "has_play", True)).replace("WIDTHxHEIGHT", f"{image_width}x{round(h * image_width / w)}")}}
    graph = graph or build_graph()
    try:  # crashes before this task began do not count against it
        before = [f"{p}:{i}" for p, i in device.crashes()] if hasattr(device, "crashes") else []
    except Exception:
        before = []
    cmd: dict | Command = {"goal": goal, "step": 0, "history": [], "acts": [], "unchanged": 0, "crash_seen": before, "crashes": {}}
    while True:
        out = graph.invoke(cmd, config)
        pending = out.get("__interrupt__")
        if not pending:
            return Result(out.get("status", "failed"), out.get("answer", ""), out.get("step", 0), out.get("history", []))
        payload = pending[0].value
        if handler is None:
            return Result("needs_approval", payload["text"], out.get("step", 0), out.get("history", []), payload)
        t_gate = time.time()
        answer = handler(payload)
        config["configurable"]["deadline"] += time.time() - t_gate  # time the owner took does not count against the task
        cmd = Command(resume=answer)
