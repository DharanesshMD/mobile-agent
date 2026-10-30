"""Orchestrator (Muse Spark): talks with the owner, remembers, asks, and delegates phone work to jobs."""
import difflib
import json
import re
import threading
import time
from pathlib import Path

from agent.checks import KINDS, run_check
from agent.config import MAX_ACTIVE_PER_BOT, ORCHESTRATOR_MODEL
from agent.db import Database
from agent.graph import CRASH_PREFIX
from agent.remote import RemoteDevice
from agent.hub import Hub

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
MAX_TOOL_LOOPS = 4
MAX_SEARCHES_PER_TURN = 3
MAX_BOT_CHAIN = 3  # the asking bot plus at most two more
MAX_PLAN_STEPS = 4
MAX_AUTO_RETRIES = 1


def same_goal(a: str, b: str) -> bool:
    """Is this the same request worded again? (Used so a repeated message does not queue the same phone task twice.)"""
    a, b = (re.sub(r"\W+", " ", x.lower()).strip() for x in (a, b))
    return bool(a) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)


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
        self.delegated: dict[str, str] = {}  # job id -> the master bot that handed it over
        self.vault, self.routines = vault, None  # routines (the scheduler) is attached by the app
        self.db, self.hub, self.model_factory, self.jobs = db, hub, model_factory, jobs
        self._locks: dict[str, threading.Lock] = {}
        self._retries: dict[str, int] = {}  # bot id -> automatic retries since the owner last wrote (a failed task may be retried once, never in a loop)
        self._origin: dict[str, str] = {}  # bot id -> the routine whose turn is running, so the tasks it starts are tagged with it
        self._finishing: set[str] = set()  # bots whose current turn was started by a finished task, not by the owner
        self._answered: dict[str, int] = {}  # bot id -> newest owner message a turn has already taken into account
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
        mine = self.db.active_jobs(bot_id)
        recent = [j for j in self.db.jobs(bot_id, 6) if j["status"] not in ("queued", "running", "waiting")][:3]
        if recent:
            parts.append("Recent phone jobs for this bot, newest first (earlier 'On it' messages in the chat refer to these):\n" +
                         "\n".join(f"- {j['id']} {j['status']}: {j['goal'][:100]}" + (f" -> {j['result'][:100]}" if j["result"] else "") for j in recent))
        if mine:
            parts.append("This bot's phone tasks right now, oldest first (the phone does one task at a time):\n" +
                         "\n".join(f"- {j['id']} [{j['status']}] {j['goal'][:100]}" + (f" (waits for {j['after']})" if j["after"] else "") for j in mine) +
                         "\nIf the owner's new message changes, adds to or cancels one of these, use steer_task or stop_task. Never add a task that repeats one listed here. "
                         "A different task goes in with mobile_task and queues behind these; if it needs an earlier task to succeed first, give that task's id as \"after\".")
        else:
            parts.append("No phone job is running for this bot right now. Never say a task is in progress or that you are 'already on it'; "
                         "if the owner asks for a phone task, start it with mobile_task, even if an earlier message in the chat said you were on it.")
        cards = self.db.open_items(bot_id)
        if cards:
            parts.append("Cards waiting for the owner in this chat:\n" + "\n".join(f"- {i['kind']}: {i['payload'].get('question', '')[:110]}" for i in cards) +
                         "\nIf the owner says they finished a 'handoff' card (\"done\", \"continue\", \"my part is done\"), use resume_task: typing in chat does not resume it by itself. "
                         "Approvals, choices, codes and secrets can only be answered on their card; tell the owner to use it.")
        if bot["kind"] == "virtual":
            _, lc = self.jobs.pair(self.jobs.lane_of(bot_id))
            parts.append("You are the bot of the GAMES PHONE: a light virtual Android (Android 13 in a container) for games and heavy apps. Every phone task you queue runs on it, "
                         "in the background, and never on the shared emulator; no other bot can use it. There is no Google Play on it: apps are installed from APK files by the owner, "
                         "so find_app tells you what is installed (do not try to install from the Play Store). Games draw one picture and have no UI tree: the worker plays from "
                         "screenshots and coordinates, so keep tasks short and concrete (one level, a few moves) and expect several tries. Some games refuse to run on virtual "
                         "devices: if one crashes or closes at start, say so plainly and suggest the owner's own phone (the Mobile Agent app) instead of retrying. "
                         f"The games phone is {(lc.info() if lc else {'power': 'unavailable'})['power']} right now (a sleeping or stopped one wakes by itself, which takes a few seconds).")
        elif bot["kind"] == "phone":
            up = bool(self.jobs.phones and self.jobs.phones.online(bot["phone_id"]))
            parts.append(f"You are the bot of the owner's OWN phone ({bot['name']}). Every phone task you queue runs on that phone, in the background, through the Mobile Agent app; "
                         "never on the shared emulator, and no other bot can use that phone. It acts on the phone's real screen, so keep tasks short and clear. "
                         + ("The phone is connected now." if up else "The phone is NOT connected right now: tasks cannot run until the owner opens the Mobile Agent app on it."))
        else:
            games = [b for b in self.db.list_bots() if b["kind"] == "virtual"]
            if games:
                parts.append("The games phone (a light virtual Android for games and heavy apps, apps installed from APK, no Google Play) has its own bot: " +
                             ", ".join(b["name"] for b in games) + ". You cannot run tasks on it; tell the owner to ask that bot" +
                             (" (Master can hand a task to it)." if bot["kind"] != "master" else ", or hand the task to it with delegate."))
            mine_phones = [b for b in self.db.list_bots() if b["kind"] == "phone"]
            if mine_phones:
                parts.append("The owner's own phone has its own bot: " + ", ".join(b["name"] for b in mine_phones) + ". You cannot run tasks on it; for something "
                             "on their own phone (games and apps that refuse to run on the emulator, their real accounts) tell them to ask that bot." +
                             (" Master can hand such a task to it." if bot["kind"] != "master" else " You can hand such a task to it with delegate."))
        lane = self.jobs.lane_of(bot_id)
        busy_with = [j for j in self.db.active_jobs() if j["bot_id"] != bot_id and self.jobs.lane_of(j["bot_id"]) == lane]
        if busy_with:
            parts.append(f"Other bots have {len(busy_with)} task(s) on the same phone; this bot's tasks queue behind them." if lane else
                         f"Other bots have {len(busy_with)} task(s) on the shared phone; this bot's tasks queue behind them.")
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
    def turn(self, bot_id: str, note: str | None = None, routine: str | None = None) -> None:
        """One orchestrator turn: read the chat, decide, act. Runs in a worker thread; never raises."""
        lock = self._locks.setdefault(bot_id, threading.Lock())
        with lock:
            if not self.db.get_bot(bot_id):
                return
            if routine:
                self._origin[bot_id] = routine
            if note is None and self._latest_owner(bot_id) <= self._answered.get(bot_id, 0):
                return  # a turn that was already running took this message into account (steering), so it was answered
            if note is None:
                self._retries[bot_id] = 0  # the owner is talking again: a new request gets its own retry
            try:
                self._turn(bot_id, note)
            except Exception as e:  # keep the chat usable if the model or network fails
                self.post(bot_id, f"Sorry, I hit a problem and could not answer ({type(e).__name__}). Please try again.")
            finally:
                self._origin.pop(bot_id, None)

    def _latest_owner(self, bot_id: str) -> int:
        rows = self.db._q("select max(id) m from messages where bot_id=? and role='user'", (bot_id,))
        return rows[0]["m"] or 0

    def _turn(self, bot_id: str, note: str | None, chain: tuple = ()) -> None:
        model = self.model_factory(ORCHESTRATOR_MODEL)
        system, msgs = self._context(bot_id), self._history(bot_id)
        seen = self._answered[bot_id] = self._latest_owner(bot_id)
        n_chat = len(msgs)  # everything after this is this turn's own notes and tool results
        if note:
            msgs = msgs + [{"role": "user", "content": f"[system note, not from the owner] {note}"}]
        searches = 0
        for _ in range(MAX_TOOL_LOOPS):
            act = self._decide(model, system, msgs)
            if self._latest_owner(bot_id) > seen:
                # The owner wrote again while the model was thinking or a tool was running: steer this turn instead of acting on a stale picture.
                seen = self._answered[bot_id] = self._latest_owner(bot_id)
                system, chat = self._context(bot_id), self._history(bot_id)
                msgs = chat + msgs[n_chat:] + [{"role": "user", "content": "[system note, not from the owner] The owner sent a new message while you were working. "
                                                "Read it above, and decide again with everything they have said so far. Answer both in one reply if they fit together."}]
                n_chat = len(chat)
                continue
            if act is None:
                self.post(bot_id, "I could not work out how to respond. Could you rephrase that?")
                return
            kind = act["type"]
            if kind == "reply":
                self.post(bot_id, str(act.get("text", "")).strip() or "Done.")
                return
            if kind == "delegate_task" and (self.db.get_bot(bot_id) or {}).get("kind") == "master":
                self._delegate(bot_id, act)
                return
            if kind in ("mobile_task", "mobile_plan"):
                self._queue_tasks(bot_id, act, kind)
                return
            if kind == "phone_check":
                msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})},
                               {"role": "user", "content": f"[tool result for phone_check] {self._phone_check(bot_id, act)}"}]
                continue
            if kind == "resume_task":
                self._resume_handoff(bot_id, act)
                return
            if kind in ("steer_task", "stop_task"):
                self._steer_job(bot_id, kind, act)
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

    @staticmethod
    def _clean_verify(v) -> dict | None:
        if isinstance(v, dict) and v.get("kind") in KINDS:
            return {"kind": v["kind"], "query": str(v.get("query", ""))[:80]}
        return None

    def _phone_check(self, bot_id: str, act: dict) -> str:
        """Read a fact straight from the phone (app installed, Google account signed in, app in front) so a task's result can be checked, not trusted."""
        dev, lc = self.jobs.device, self.jobs.lifecycle
        own = self.db.get_bot(bot_id) or {}
        if own.get("kind") == "virtual":  # a fact from the games phone
            dev, lc = self.jobs.pair(self.jobs.lane_of(bot_id))
            if dev is None:
                return "error: the games phone is not set up"
        elif own.get("kind") == "phone":  # a fact from the owner's own phone
            pid = self.jobs.phones.pick(own.get("phone_id")) if self.jobs.phones else None
            if not pid:
                return "error: the owner's phone is not connected"
            dev, lc = RemoteDevice(self.jobs.phones, pid), None
        if not hasattr(dev, "packages"):
            return "error: phone checks are not available"
        if lc is not None and not lc.ensure_healthy(90):
            return "error: the phone is not available right now (it is starting). Start the task instead; it waits for the phone."
        ok, detail = run_check(dev, act)
        return f"{'yes' if ok else 'no'}: {detail}"

    def _queue_tasks(self, bot_id: str, act: dict, kind: str) -> None:
        """mobile_task / mobile_plan: queue one phone task, or several in order. Repeats are ignored, a step that needs an earlier one waits for it."""
        raw = act.get("steps") if kind == "mobile_plan" else [act]
        steps = [st for st in (raw if isinstance(raw, list) else []) if isinstance(st, dict) and str(st.get("goal", "")).strip()][:MAX_PLAN_STEPS]
        if not steps:
            self.post(bot_id, "What should I do on the phone?")
            return
        if bot_id in self._finishing:  # this turn follows a finished task: starting another is a retry, and a retry happens once
            if self._retries.get(bot_id, 0) >= MAX_AUTO_RETRIES:
                self.post(bot_id, "I already retried this once and it still did not finish, so I'm stopping here instead of looping. "
                                  "Tell me how you'd like to go on (for example: keep trying, or do something else).")
                return
            self._retries[bot_id] = self._retries.get(bot_id, 0) + 1
        active, prev = self.db.active_jobs(bot_id), None
        if act.get("after"):
            dep = self.db.get_job(str(act["after"]))
            if dep and dep["bot_id"] == bot_id and dep["status"] in ("queued", "running", "waiting"):
                prev = dep["id"]
            elif dep and dep["bot_id"] == bot_id and dep["status"] != "done":
                self.post(bot_id, f"I didn't start that: it needs an earlier task that did not finish ({dep['status']}): {dep['goal'][:80]}. Should I try that one again first?")
                return
        new, known, chain = [], list(active), prev
        own = self.db.get_bot(bot_id) or {}
        if own.get("kind") == "phone" and not (self.jobs.phones and self.jobs.phones.online(own.get("phone_id"))):
            self.post(bot_id, f"Your phone ({own['name']}) is not connected right now. Open the Mobile Agent app on it and let it run in the background, then ask me again.")
            return  # chain: what the next new step waits for (an existing job id, or the index of an earlier new step)
        for st in steps:
            goal = str(st["goal"]).strip()
            dup = next((j for j in known if same_goal(j["goal"], goal)), None)
            if dup:
                chain = dup["id"] if dup["id"] else chain  # a later step still waits for the task that already exists
                continue
            if len(known) >= MAX_ACTIVE_PER_BOT:
                break
            new.append((goal, self._clean_verify(st.get("verify")), chain))
            known.append({"id": None, "goal": goal})
            chain = len(new) - 1
        if not new:
            self.post(bot_id, "That is already running or queued, so I did not add it again. I'll report back when it finishes." if len(known) < MAX_ACTIVE_PER_BOT
                      else "I already have several tasks queued. I'll work through them and then we can add more.")
            return
        say = str(act.get("say", "")).strip() or ("On it. I'll let you know when it's done." if len(new) == 1 else
                                                  f"I'll do this in {len(new)} steps, in order, and tell you how it goes.")
        if self.jobs.busy_for_bot(bot_id):
            say += " The phone is busy, so it's queued and starts as soon as it's free."
        self.post(bot_id, say)
        ids: list[str] = []
        for goal, verify, dep in new:
            ids.append(self.jobs.submit(bot_id, goal, ids[dep] if isinstance(dep, int) else dep, verify, self._origin.get(bot_id))["id"])

    def _resume_handoff(self, bot_id: str, act: dict) -> None:
        """The owner says, in chat, that they finished the part they took over on the phone: answer the handoff card for them so the task goes on.
        Only handoffs: an approval, choice, code or secret must be given on its own card, never inferred from a loose "done"."""
        item = next((i for i in self.db.open_items(bot_id) if i["kind"] == "handoff" and i["job_id"]), None)
        if not item:
            self.post(bot_id, "Nothing is waiting for you to finish on the phone. If a card is open, use it to approve, choose or enter a code.")
            return
        if not self.jobs.resume(item["job_id"], {"done": True}):
            self.post(bot_id, "That task is no longer waiting for you.")
            return
        self.db.close_item(item["id"], "answered", "Done")
        m = self.db.get_message(item["message_id"]) if item["message_id"] else None
        if m:
            self.hub.publish(bot_id, {"type": "message", "message": self.db.update_message(m["id"], meta={**m["meta"], "status": "answered", "answer": "Done"})})
        self.jobs.release_control()  # they are no longer driving the phone
        self.post(bot_id, str(act.get("say", "")).strip() or "Thanks, I'm continuing the task now.")

    def _steer_job(self, bot_id: str, kind: str, act: dict) -> None:
        """The owner changed their mind about a task that is already queued or running."""
        job = self.jobs.active_for(bot_id)
        named = self.db.get_job(str(act.get("job", ""))) if act.get("job") else None
        if named and named["bot_id"] == bot_id and named["status"] in ("queued", "running", "waiting"):
            job = named
        if not job:
            self.post(bot_id, "There's no task running right now. Tell me what you'd like me to do on the phone and I'll start it.")
            return
        if kind == "stop_task":
            self.jobs.cancel(job["id"])
            self.post(bot_id, str(act.get("say", "")).strip() or "Stopping the task.")
            return
        note = str(act.get("note", "")).strip()
        if not note or not self.jobs.steer(job["id"], note):
            self.post(bot_id, "I couldn't pass that on to the task. What should it do differently?")
            return
        self.post(bot_id, str(act.get("say", "")).strip() or "Got it, I've passed that to the phone. It will use it on its next step.")

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

    def _delegate(self, master_id: str, act: dict) -> None:
        """The master hands a phone task to the bot that fits it. The task runs, and is reported, in that bot's chat; the master hears back too."""
        goal, say = str(act.get("goal", "")).strip(), str(act.get("say", "")).strip()
        if not goal:
            self.post(master_id, "What should I get done on the phone?")
            return
        target = self._find_bot(str(act.get("bot", "")), (master_id,))
        if isinstance(target, str):
            self.post(master_id, target.replace("that you can ask now", "to hand this to") + ". I can run it myself instead if you like.")
            return
        active = self.db.active_jobs(target["id"])
        if any(same_goal(j["goal"], goal) for j in active):
            self.post(master_id, f"{target['name']} is already working on that, so I did not add it again.")
            return
        if len(active) >= MAX_ACTIVE_PER_BOT:
            self.post(master_id, f"{target['name']} already has {len(active)} tasks queued. I can run this one myself, or wait for them to finish.")
            return
        self.post(master_id, (say or f"I asked {target['name']} to do that. Watch its chat for questions; I'll tell you when it's done.") +
                  (" The phone is busy, so it's queued." if self.jobs.busy_for_bot(target["id"]) else ""))
        self.post(target["id"], f"Master asked you to do this on the phone: {goal}", "bot_note", {"with": master_id})
        job = self.jobs.submit(target["id"], goal)
        self.delegated[job["id"]] = master_id

    def job_finished(self, bot_id: str, job: dict) -> None:
        master_id = self.delegated.pop(job["id"], None)
        if master_id:
            bot = self.db.get_bot(bot_id) or {"name": "A bot"}
            text = {"done": f"{bot['name']} finished: {job['goal']}\n{job['result']}", "cancelled": f"{bot['name']}'s task was stopped: {job['goal']}"}.get(
                job["status"], f"{bot['name']}'s task did not complete ({job['status']}): {job['goal']}\n{job['result']}")
            self.post(master_id, text, "bot_note", {"with": bot_id})
        status, result = job["status"], job["result"]
        if job.get("routine") and self.db.get_routine(job["routine"]):  # the routine list shows how its last run went
            self.db.routine_update(job["routine"], last_status=status)
            if self.routines:
                self.routines.on_change(bot_id)
        if status == "cancelled":
            self.post(bot_id, "Stopped the task.")
            return
        if status == "done":
            self._retries[bot_id] = 0
            self.db.memory_add(bot_id, f"Task done: {job['goal']} -> {result}")
        waiting = [j for j in self.db.active_jobs(bot_id) if j["after"] == job["id"]]
        queued = self.db.active_jobs(bot_id)
        crashed = status == "failed" and result.startswith(CRASH_PREFIX)
        if crashed:  # retrying a crashing app only repeats the crash: say so, remember it, and let the owner decide
            self._retries[bot_id] = MAX_AUTO_RETRIES
            self.db.memory_add(bot_id, f"Task failed: {job['goal']} -> {result}")
        if status == "skipped":
            self._turn_with(bot_id, f"A queued phone task was skipped, because the task before it did not finish. Goal: {job['goal']}. {result}. "
                                    "Tell the owner in one sentence that it did not run and why, and offer to retry the earlier task first.", result)
            return
        note = (f"The phone job finished with status '{status}'. Goal: {job['goal']}. Worker report: {result or '(none)'}.\n"
                "First decide whether the goal was REALLY achieved: the report has to show it, and 'done' alone is not proof. If it does not prove it "
                "(for example an install that was only tapped, or a sign-in the worker did not confirm), check with phone_check (app_installed, account or foreground) "
                "before you say so. Then tell the owner in one or two sentences using reply: plainly whether it worked, partly worked or failed. "
                "If it failed or is incomplete for a fixable reason you may start ONE retry with a clearer goal (mobile_task), and say that you are retrying.")
        if crashed:
            note += (" The app crashed on its own several times, so do NOT start a retry and do NOT promise to try again. Tell the owner plainly that this app "
                     "crashes on the phone and why (see the report), and offer an alternative only if one really exists (for example running it on the owner's own phone). Do not invent one.")
        if status == "max_steps":
            note += (" The worker ran out of its step or time budget, so the task is probably only partly done (a long download or install may still be running on the phone). "
                     "Check the real state with phone_check, and if the goal is not met start ONE retry that continues from where the phone is now, saying that you are continuing.")
        if status != "done" and waiting:
            note += f" {len(waiting)} queued task(s) depended on this one and will be skipped; say so."
        elif queued:
            note += f" {len(queued)} more task(s) for this bot are still queued and start by themselves; mention that briefly."
        self._turn_with(bot_id, note, f"Task {status}: {result or job['goal']}", job.get("routine"))

    def _turn_with(self, bot_id: str, note: str, fallback: str, routine: str | None = None) -> None:
        """A system-note turn (a job finished or was skipped) under the bot's lock; if the model fails the owner still gets the plain outcome."""
        with self._locks.setdefault(bot_id, threading.Lock()):
            self._finishing.add(bot_id)
            if routine:
                self._origin[bot_id] = routine  # a retry of a routine's task still belongs to that routine
            try:
                self._turn(bot_id, note)
            except Exception:
                self.post(bot_id, fallback, "result")
            finally:
                self._finishing.discard(bot_id)
                self._origin.pop(bot_id, None)

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
