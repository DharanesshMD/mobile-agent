"""Orchestrator (Muse Spark): talks with the owner, remembers, asks, and delegates phone work to jobs."""
import json
import re
import threading
from pathlib import Path

from agent.config import ORCHESTRATOR_MODEL
from agent.db import Database
from agent.hub import Hub

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
MAX_TOOL_LOOPS = 4


def parse_decision(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object")
    obj = json.loads(m.group(0))
    act = obj.get("action")
    if not isinstance(act, dict) or "type" not in act:
        raise ValueError("missing action.type")
    return act


class Orchestrator:
    def __init__(self, db: Database, hub: Hub, model_factory, jobs, vault=None):
        self.vault = vault
        self.db, self.hub, self.model_factory, self.jobs = db, hub, model_factory, jobs
        self._locks: dict[str, threading.Lock] = {}
        self._system = (PROMPTS / "orchestrator.md").read_text()
        self._worker_system = (PROMPTS / "worker_request.md").read_text()

    # --- helpers -------------------------------------------------------------------------------------------------
    def post(self, bot_id: str, text: str, kind: str = "text", meta: dict | None = None) -> dict:
        msg = self.db.add_message(bot_id, "assistant", kind, text, meta)
        self.hub.publish(bot_id, {"type": "message", "message": msg})
        return msg

    def make_item(self, bot_id: str, job_id: str | None, spec: dict) -> dict:
        """Create an action item and its chat card. spec: kind, question, options?, field?"""
        kind = spec.get("kind") if spec.get("kind") in ("choice", "confirm", "text", "handoff", "otp") else "text"
        options = [str(o)[:80] for o in (spec.get("options") or [])][:4]
        if kind == "choice" and len(options) < 2:
            kind = "text"
        payload = {"kind": kind, "question": str(spec.get("question", ""))[:500], "options": options, "field": spec.get("field")}
        item = self.db.create_item(bot_id, job_id, kind, payload)
        msg = self.post(bot_id, payload["question"], "action_item", {"item_id": item["id"], "status": "open", **payload})
        self.db.set_item_message(item["id"], msg["id"])
        return self.db.get_item(item["id"])

    def _history(self, bot_id: str) -> list[dict]:
        out: list[dict] = []
        for m in self.db.messages(bot_id, 30):
            if m["kind"] in ("text", "result"):
                out.append({"role": "user" if m["role"] == "user" else "assistant", "content": m["content"]})
            elif m["kind"] == "action_item" and m["meta"].get("status") == "answered":
                out.append({"role": "assistant", "content": f"(asked: {m['content']})"})
        return out or [{"role": "user", "content": "(no messages yet)"}]

    def _context(self, bot_id: str) -> str:
        bot = self.db.get_bot(bot_id)
        parts = [self._system]
        if bot["persona"]:
            parts.append("Persona and instructions for this bot:\n" + bot["persona"])
        labels = self.vault.labels() if self.vault else []
        if labels:
            parts.append("Saved secrets (labels only, you never see the values; the phone worker can type one after the owner approves): " + ", ".join(labels))
        active = self.jobs.active_for(bot_id)
        if active:
            parts.append(f"A phone job is already {active['status']} for this bot: {active['goal']}")
        return "\n\n".join(parts)

    def _decide(self, model, system: str, msgs: list[dict]) -> dict | None:
        for attempt in range(2):
            raw = model.chat(system, msgs)
            try:
                return parse_decision(raw)
            except (ValueError, json.JSONDecodeError):
                msgs = msgs + [{"role": "assistant", "content": raw},
                               {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."}]
        return None

    # --- turns ---------------------------------------------------------------------------------------------------
    def turn(self, bot_id: str, note: str | None = None) -> None:
        """One orchestrator turn: read the chat, decide, act. Runs in a worker thread; never raises."""
        lock = self._locks.setdefault(bot_id, threading.Lock())
        with lock:
            try:
                self._turn(bot_id, note)
            except Exception as e:  # keep the chat usable if the model or network fails
                self.post(bot_id, f"Sorry, I hit a problem and could not answer ({type(e).__name__}). Please try again.")

    def _turn(self, bot_id: str, note: str | None) -> None:
        model = self.model_factory(ORCHESTRATOR_MODEL)
        system, msgs = self._context(bot_id), self._history(bot_id)
        if note:
            msgs = msgs + [{"role": "user", "content": f"[system note, not from the owner] {note}"}]
        for _ in range(MAX_TOOL_LOOPS):
            act = self._decide(model, system, msgs)
            if act is None:
                self.post(bot_id, "I could not work out how to respond. Could you rephrase that?")
                return
            kind = act["type"]
            if kind == "reply":
                self.post(bot_id, str(act.get("text", "")).strip() or "Done.")
                return
            if kind == "mobile_task":
                goal = str(act.get("goal", "")).strip()
                say = str(act.get("say", "")).strip() or "On it. I'll let you know when it's done."
                if not goal:
                    self.post(bot_id, "What should I do on the phone?")
                    return
                if self.jobs.active_for(bot_id):
                    self.post(bot_id, "I'm already working on a task for you. I'll report back when it finishes.")
                    return
                self.post(bot_id, say)
                self.jobs.submit(bot_id, goal)
                return
            if kind == "ask_user":
                self.make_item(bot_id, None, act)
                return
            if kind in ("memory_search", "memory_save"):
                if kind == "memory_save":
                    self.db.memory_add(bot_id, str(act.get("text", "")))
                    result = "saved"
                else:
                    hits = self.db.memory_search(bot_id, str(act.get("query", "")))
                    result = "; ".join(hits) if hits else "nothing found"
                msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                               {"role": "user", "content": f"[tool result for {kind}] {result}"}]
                continue
            self.post(bot_id, str(act.get("text", "")).strip() or "I'm not sure how to do that yet.")
            return
        self.post(bot_id, "I got stuck deciding what to do. Could you tell me more?")

    def job_finished(self, bot_id: str, job: dict) -> None:
        status, result = job["status"], job["result"]
        if status == "cancelled":
            self.post(bot_id, "Stopped the task.")
            return
        if status == "done":
            self.db.memory_add(bot_id, f"Task done: {job['goal']} -> {result}")
        note = (f"The phone job finished with status '{status}'. Goal: {job['goal']}. Worker report: {result or '(none)'}. "
                "Tell the owner the outcome in one or two sentences using reply. If it failed, say what went wrong and offer a next step.")
        with self._locks.setdefault(bot_id, threading.Lock()):
            try:
                self._turn(bot_id, note)
            except Exception:
                self.post(bot_id, f"Task {status}: {result or job['goal']}", "result")

    def worker_request(self, bot_id: str, job: dict, payload: dict) -> dict:
        """The worker needs a plain value. Try memory and the model first; otherwise ask the owner in chat.
        Returns {"value": str} or {"item": <action item>} (the owner's answer then resumes the worker)."""
        field, why = payload.get("field", ""), payload.get("text", "")
        hits = self.db.memory_search(bot_id, f"{field} {why}")
        user = (f"Job goal: {job['goal']}\nThe worker needs: {field}\nWhy: {why}\n"
                f"Memory hits: {'; '.join(hits) if hits else 'none'}")
        msgs = [{"role": "user", "content": user}]
        try:
            model = self.model_factory(ORCHESTRATOR_MODEL)
            for _ in range(3):
                act = self._decide(model, self._worker_system, msgs)
                if not act:
                    break
                if act["type"] == "answer_worker" and str(act.get("value", "")).strip():
                    return {"value": str(act["value"]).strip()}
                if act["type"] == "memory_search":
                    more = self.db.memory_search(bot_id, str(act.get("query", "")))
                    msgs += [{"role": "assistant", "content": json.dumps({"action": act})},
                             {"role": "user", "content": "[tool result] " + ("; ".join(more) if more else "nothing found")}]
                    continue
                if act["type"] == "ask_user":
                    return {"item": self.make_item(bot_id, job["id"], {**act, "field": field})}
                break
        except Exception:
            pass
        question = f"I need your {field or 'input'} to continue" + (f" ({why})" if why else "") + "."
        return {"item": self.make_item(bot_id, job["id"], {"kind": "text", "question": question, "field": field})}
