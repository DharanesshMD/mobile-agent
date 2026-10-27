"""LangGraph orchestration: observe -> plan -> (act | gate | finish), with owner pause points.

Gates (ask, handoff, request_otp) call interrupt(), so a run can wait for the owner for any length of time
and resume exactly where it stopped. Add more agents later as extra nodes or sub-graphs on the same state.
"""
import hashlib
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent.device import Element
from agent.loop import GATES, SHOT_WIDTH, SYSTEM, build_prompt, execute, parse_action, shrink
from agent.loop import settle as settle_screen


class State(TypedDict, total=False):
    goal: str
    step: int
    history: list[str]
    acts: list[dict]
    last_hash: str
    unchanged: int
    obs: dict
    action: dict
    thought: str
    status: str
    answer: str


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


def observe(state: State, config: RunnableConfig) -> dict:
    c = _c(config)
    if c.get("pause"):
        c["pause"]()  # blocks while the owner holds control of the phone
    if c["cancel"].is_set():
        return {"status": "cancelled"}
    if state.get("step", 0) >= c["max_steps"]:
        return {"status": "max_steps", "answer": "step budget exhausted"}
    dev = c["device"]
    png = dev.screenshot()
    digest = hashlib.sha1(png).hexdigest()
    unchanged = state.get("unchanged", 0) + 1 if digest == state.get("last_hash") else 0
    small, scale = shrink(png)
    elements = dev.elements()
    prompt = build_prompt(state["goal"], state.get("history", []), elements, scale, unchanged, state.get("acts", []))
    return {"step": state.get("step", 0) + 1, "last_hash": digest, "unchanged": unchanged,
            "obs": {"png": small, "scale": scale, "elements": [asdict(e) for e in elements], "prompt": prompt}}


def plan(state: State, config: RunnableConfig) -> dict:
    c, obs = _c(config), state["obs"]
    prompt, parsed = obs["prompt"], None
    for _ in range(2):
        reply = c["model"].complete(c["system"], prompt, obs["png"])
        try:
            parsed = parse_action(reply)
            break
        except (ValueError, json.JSONDecodeError):
            prompt += "\n\nYour last reply was not valid JSON. Reply with only the JSON object."
    if parsed is None:
        return {"status": "failed", "answer": "model returned invalid JSON twice"}
    act, thought, n = parsed["action"], str(parsed.get("thought", "")), state["step"]
    if c.get("trace"):
        t = Path(c["trace"])
        t.mkdir(parents=True, exist_ok=True)
        (t / f"step{n:02d}.png").write_bytes(obs["png"])
        (t / f"step{n:02d}.json").write_text(json.dumps({"prompt": obs["prompt"], "reply": parsed}, indent=2))
    _emit(config, {"type": "step", "n": n, "thought": thought, "action": act})
    return {"action": act, "thought": thought, "acts": state.get("acts", []) + [act]}


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
    if kind == "need_info":
        payload = {"kind": kind, "field": str(act.get("field", "")), "text": str(act.get("why", ""))}
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
    else:
        code = str(resume.get("code") or "")
        if not code:
            return {"status": "denied", "answer": "owner did not share a code"}
        c["device"].type_text(code)  # typed here only: never put in state, history, events or the model prompt
        history = history + [f"{n}. one-time code entered"]
    settle_screen(c["device"], c["settle"])
    return {"history": history}


def act(state: State, config: RunnableConfig) -> dict:
    c, obs, a, n = _c(config), state["obs"], state["action"], state["step"]
    elements = [Element(e["id"], e["label"], tuple(e["center"]), e["clickable"]) for e in obs["elements"]]
    try:
        execute(c["device"], a, elements, obs["scale"], c["size"])
        outcome = "ok"
    except Exception as e:  # bad element id, adb error: tell the model and carry on
        outcome = f"error: {e}"
    _emit(config, {"type": "result", "n": n, "outcome": outcome})
    settle_screen(c["device"], c["settle"])
    return {"history": state.get("history", []) + [f"{n}. {json.dumps(a)} -> {outcome}"]}


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
        on_step: Callable[[int, dict, str], None] | None = None, settle: float = 0.6,
        handler: Callable[[dict], dict] | None = None, emit: Callable[[dict], None] | None = None,
        cancel: threading.Event | None = None, graph=None, pause: Callable[[], None] | None = None) -> Result:
    """Run a goal to completion. Without a handler, the run stops at the first gate with status needs_approval."""
    w, h = device.size()

    def fan_out(event: dict) -> None:
        if event["type"] == "step" and on_step:
            on_step(event["n"], event["action"], event["thought"])
        if emit:
            emit(event)

    config = {"recursion_limit": max_steps * 6 + 20, "configurable": {
        "thread_id": uuid.uuid4().hex, "device": device, "model": model, "size": (w, h), "settle": settle,
        "max_steps": max_steps, "trace": str(trace_dir) if trace_dir else None, "emit": fan_out,
        "cancel": cancel or threading.Event(), "pause": pause,
        "system": SYSTEM.replace("WIDTHxHEIGHT", f"{SHOT_WIDTH}x{round(h * SHOT_WIDTH / w)}")}}
    graph = graph or build_graph()
    cmd: dict | Command = {"goal": goal, "step": 0, "history": [], "acts": [], "unchanged": 0}
    while True:
        out = graph.invoke(cmd, config)
        pending = out.get("__interrupt__")
        if not pending:
            return Result(out.get("status", "failed"), out.get("answer", ""), out.get("step", 0), out.get("history", []))
        payload = pending[0].value
        if handler is None:
            return Result("needs_approval", payload["text"], out.get("step", 0), out.get("history", []), payload)
        cmd = Command(resume=handler(payload))
