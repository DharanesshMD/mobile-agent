"""Orchestrator (Muse Spark): talks with the owner, remembers, asks, and delegates phone work to jobs."""
import json
import re
import threading
import time
from pathlib import Path

from agent.config import ORCHESTRATOR_MODEL
from agent.db import Database
from agent.hub import Hub

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
MAX_TOOL_LOOPS = 4
MAX_SEARCHES_PER_TURN = 3
MAX_BOT_CHAIN = 3  # the asking bot plus at most two more


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
        self.vault, self.routines = vault, None  # routines (the scheduler) is attached by the app
        self.db, self.hub, self.model_factory, self.jobs = db, hub, model_factory, jobs
        self._locks: dict[str, threading.Lock] = {}
        self._system = (PROMPTS / "orchestrator.md").read_text()
        self._worker_system = (PROMPTS / "worker_request.md").read_text()

    # --- helpers -------------------------------------------------------------------------------------------------
    def post(self, bot_id: str, text: str, kind: str = "text", meta: dict | None = None) -> dict:
        if not self.db.get_bot(bot_id):  # the bot was deleted while a turn or job was finishing
            return {"id": 0, "role": "assistant", "kind": kind, "content": text, "meta": meta or {}}
        msg = self.db.add_message(bot_id, "assistant", kind, text, meta)
        self.hub.publish(bot_id, {"type": "message", "message": msg})
        return msg

    def make_item(self, bot_id: str, job_id: str | None, spec: dict) -> dict:
        """Create an action item and its chat card. spec: kind, question, options?, field?"""
        kind = spec.get("kind") if spec.get("kind") in ("choice", "confirm", "text", "handoff", "otp", "secret") else "text"
        options = [str(o)[:80] for o in (spec.get("options") or [])][:4]
        if kind == "choice" and len(options) < 2:
            kind = "text"
        payload = {"kind": kind, "question": str(spec.get("question", ""))[:500], "options": options, "field": spec.get("field")}
        if kind == "secret":
            sk = str(spec.get("secret_kind") or "password")
            payload.update(label=str(spec.get("label") or "")[:60], secret_kind=sk if sk in ("password", "card", "id", "note") else "note")
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
        parts = [self._system, f"Today is {time.strftime('%A %d %B %Y')} (server local time)."]
        if bot["persona"]:
            parts.append("Persona and instructions for this bot:\n" + bot["persona"])
        labels = self.vault.labels() if self.vault else []
        if labels:
            parts.append("Saved secrets (labels only, you never see the values; the phone worker can type one after the owner approves): " + ", ".join(labels))
        if bot["kind"] == "master":
            from agent.master import digest
            parts.append((PROMPTS / "master.md").read_text())
            parts.append("Workspace digest (now):\n" + digest(self.db))
        others = [b for b in self.db.list_bots() if b["id"] != bot_id]
        if others:
            parts.append("Other bots you can ask with ask_bot (they answer from their own memory and focus):\n" +
                         "\n".join(f"- {b['name']}" + (f": {b['persona'][:120]}" if b["persona"] else "") for b in others))
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
            if not self.db.get_bot(bot_id):
                return
            try:
                self._turn(bot_id, note)
            except Exception as e:  # keep the chat usable if the model or network fails
                self.post(bot_id, f"Sorry, I hit a problem and could not answer ({type(e).__name__}). Please try again.")

    def _turn(self, bot_id: str, note: str | None, chain: tuple = ()) -> None:
        model = self.model_factory(ORCHESTRATOR_MODEL)
        system, msgs = self._context(bot_id), self._history(bot_id)
        if note:
            msgs = msgs + [{"role": "user", "content": f"[system note, not from the owner] {note}"}]
        searches = 0
        for _ in range(MAX_TOOL_LOOPS):
            act = self._decide(model, system, msgs)
            if act is None:
                self.post(bot_id, "I could not work out how to respond. Could you rephrase that?")
                return
            kind = act["type"]
            if kind == "reply":
                self.post(bot_id, str(act.get("text", "")).strip() or "Done.")
                return
            if kind == "mobile_task" and (self.db.get_bot(bot_id) or {}).get("kind") == "master":
                self.post(bot_id, "I don't run phone tasks myself. Ask one of your other bots to do it (I can tell you which fits best), or create a new bot for it.")
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
            if kind == "web_search":
                searches += 1
                result = self._web_search(model, bot_id, str(act.get("query", "")), searches)
                msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                               {"role": "user", "content": f"[tool result for web_search] {result}"}]
                continue
            if kind == "ask_bot":
                msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                               {"role": "user", "content": f"[tool result for ask_bot] {self._ask_bot(bot_id, act, chain or (bot_id,))}"}]
                continue
            if kind == "request_secret":
                label = str(act.get("label", "")).strip()
                if label and self.vault and self.vault.find(label):
                    msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                                   {"role": "user", "content": f"[tool result for request_secret] '{label}' is already saved; no need to ask"}]
                    continue
                self.make_item(bot_id, None, {**act, "kind": "secret", "question": act.get("question") or
                                              f"Enter your {label or 'secret'} to save it securely. It is encrypted and never shown again."})
                return
            if kind == "vault_check":
                label = str(act.get("label", "")).strip()
                hit = self.vault.find(label) if self.vault and label else None
                have = ", ".join(self.vault.labels()) if self.vault else ""
                result = f"'{hit['label']}' is saved ({hit['kind']})" if hit else f"'{label}' is not saved; saved: {have or 'nothing'}"
                msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                               {"role": "user", "content": f"[tool result for vault_check] {result}"}]
                continue
            if kind in ("routine_create", "routine_list", "routine_delete"):
                msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                               {"role": "user", "content": f"[tool result for {kind}] {self._routine_tool(bot_id, kind, act)}"}]
                continue
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

    def _routine_tool(self, bot_id: str, kind: str, act: dict) -> str:
        from agent.routines import describe
        if self.routines is None:
            return "routines are not available"
        try:
            if kind == "routine_create":
                r = self.routines.add(bot_id, str(act.get("name", "")), str(act.get("prompt", "")), act.get("schedule") or {})
                return f"created '{r['name']}' ({describe(r['schedule'])}); tell the owner it is set"
            rows = self.db.routines(bot_id)
            if kind == "routine_list":
                return "; ".join(f"{r['name']} ({describe(r['schedule'])}{'' if r['enabled'] else ', paused'})" for r in rows) or "no routines"
            name = str(act.get("name", "")).strip().lower()
            hit = next((r for r in rows if r["name"].lower() == name), None)
            if not hit:
                return f"no routine named '{act.get('name', '')}'; existing: " + (", ".join(r["name"] for r in rows) or "none")
            self.db.routine_delete(hit["id"])
            self.routines.on_change(bot_id)
            return f"deleted '{hit['name']}'"
        except ValueError as e:
            return f"error: {e}"

    def _web_search(self, model, bot_id: str, query: str, count: int) -> str:
        """Meta's web search grounding through the Responses API. Returns the grounded answer plus source URLs for the model to cite."""
        query = " ".join(query.split())[:300]
        if len(query) < 3:
            return "error: a search query is required"
        if count > MAX_SEARCHES_PER_TURN:
            return "error: search limit for this turn reached; answer with what you have"
        if not hasattr(model, "search"):
            return "error: web search is not available"
        self.post(bot_id, f"Searched the web: {query}", "bot_note", {"search": query})
        try:
            r = model.search(query)
        except Exception as e:
            return f"error: the search failed ({type(e).__name__}); say so and answer from what you know"
        sources = "\n".join(f"- {s['title'] or s['url']}: {s['url']}" for s in r["sources"][:5])
        return f"{r['text'] or '(no answer text)'}\n\nSources:\n{sources or '(none)'}"

    def _find_bot(self, name: str, exclude: tuple) -> dict | str:
        """A bot by name (exact, else unique prefix), or an error string for the model."""
        name = name.strip().lower()
        others = [b for b in self.db.list_bots() if b["id"] not in exclude]
        hit = [b for b in others if b["name"].lower() == name] or [b for b in others if name and b["name"].lower().startswith(name)]
        if len(hit) == 1:
            return hit[0]
        names = ", ".join(b["name"] for b in others) or "none"
        return f"no unique bot named '{name}' that you can ask now; available: {names}"

    def _ask_bot(self, bot_id: str, act: dict, chain: tuple) -> str:
        """Ask another bot one question and get its answer back (it answers from its own persona and memory; no phone, no owner prompts).
        Both chats get a note so the owner can see the exchange. Chains are limited to three bots and never loop back."""
        question = str(act.get("question", "")).strip()
        if not question:
            return "error: a question is required"
        if len(chain) >= MAX_BOT_CHAIN:
            return "error: too many bots in a row; answer with what you know"
        target = self._find_bot(str(act.get("bot", "")), chain)
        if isinstance(target, str):
            return target
        me = self.db.get_bot(bot_id)
        try:
            answer = self._answer_bot(target["id"], me["name"], question, chain + (target["id"],))
        except Exception as e:
            return f"error: {target['name']} could not answer ({type(e).__name__})"
        self.post(bot_id, f"You asked {target['name']}: {question}\n{target['name']}: {answer}", "bot_note", {"with": target["id"]})
        self.post(target["id"], f"{me['name']} asked: {question}\nYou answered: {answer}", "bot_note", {"with": bot_id})
        return f"{target['name']} answered: {answer}"

    def _answer_bot(self, bot_id: str, from_name: str, question: str, chain: tuple) -> str:
        model = self.model_factory(ORCHESTRATOR_MODEL)
        system = self._context(bot_id) + ("\n\nAnother bot, not the owner, is asking you something. Answer it with reply, briefly and only from what you know "
                                          "(your memory and focus). You cannot start phone tasks, ask the owner, or use the vault here.")
        hits = self.db.memory_search(bot_id, question)  # look first, so a bot does not say "I don't know" about what it remembers
        msgs = [{"role": "user", "content": f"[question from the bot '{from_name}', not from the owner] {question}\n"
                                            f"Your memory matches: {'; '.join(hits) if hits else 'none'}"}]
        for _ in range(MAX_TOOL_LOOPS):
            act = self._decide(model, system, msgs)
            if act is None:
                return "I could not work out an answer."
            kind = act["type"]
            if kind == "reply":
                return str(act.get("text", "")).strip() or "I have nothing to add."
            if kind == "memory_search":
                hits = self.db.memory_search(bot_id, str(act.get("query", "")))
                result = "; ".join(hits) if hits else "nothing found"
            elif kind == "ask_bot":
                result = self._ask_bot(bot_id, act, chain)
            else:
                result = "not available while answering another bot; reply with what you know or say what is missing"
            msgs += [{"role": "assistant", "content": json.dumps({"action": act})}, {"role": "user", "content": f"[tool result for {kind}] {result}"}]
        return "I could not settle on an answer."

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
