"""Orchestrator (Muse Spark): talks with the owner, remembers, asks, and delegates phone work to jobs."""
import difflib
import json
import re
import threading
import time
from pathlib import Path

from agent import phones
from agent.checks import KINDS, find_packages, run_check
from agent.config import MAX_ACTIVE_PER_BOT, ORCHESTRATOR_MODEL
from agent.db import Database
from agent.escalate import SIMPLE_OPEN, Escalator, app_name
from agent import gamepolicy, images, options, websearch
from agent.graph import CRASH_PREFIX
from agent.moveapp import move_app
from agent.remote import RemoteDevice
from agent.hub import Hub
from agent.model import ModelError

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
MAX_TOOL_LOOPS = 4
MAX_SEARCHES_PER_TURN = 3
MAX_BOT_CHAIN = 3  # the asking bot plus at most two more
MAX_PLAN_STEPS = 4
MAX_AUTO_RETRIES = 1

MAX_REROUTES = 2  # a task Master handed over that fails may be sent to another phone twice; after that Master only explains


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
        self._reroutes: dict[str, int] = {}  # master bot id -> automatic reroutes started for the owner's current request
        self._attempts: dict[str, list[tuple[str, str]]] = {}  # master bot id -> (bot id, goal) already tried for the current request
        self._explain_only: set[str] = set()  # master bots that used up their reroutes: they may only explain now
        self._summarize: set[str] = set()  # master bots that moved an app or rerouted a task: when the handed-over task finishes, Master gives the owner the final answer
        self.escalator = Escalator(self)  # an app that cannot run on a phone is tried on the others, in code (agent/escalate.py)
        self._system = (PROMPTS / "orchestrator.md").read_text()
        self._worker_system = (PROMPTS / "worker_request.md").read_text()
        self.web_provider, self.web_fetch = None, None  # the search provider and page reader (None: the real ones); tests give fakes
        self.web_in_chain = False  # the escalation chain ends in a web_options card (switched on by the real app, never in tests)

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
        web = spec.get("web") if isinstance(spec.get("web"), dict) else None  # a card from web_options: its picks are handled in code (web_pick), never by the model
        options = [str(o)[:80] for o in (spec.get("options") or [])][:6 if web else 4]
        if kind == "choice" and len(options) < 2:
            kind = "text"
        payload = {"kind": kind, "question": str(spec.get("question", ""))[:3000 if web else 500], "options": options, "field": spec.get("field")}
        if web:
            payload["web"] = web
        if kind == "secret":
            sk = str(spec.get("secret_kind") or "password")
            payload.update(label=str(spec.get("label") or "")[:60], secret_kind=sk if sk in ("password", "card", "id", "note") else "note")
        item = self.db.create_item(bot_id, job_id, kind, payload)
        msg = self.post(bot_id, payload["question"], "action_item", {"item_id": item["id"], "status": "open", **payload})
        self.db.set_item_message(item["id"], msg["id"])
        return self.db.get_item(item["id"])

    MODEL_PICTURE_MESSAGES = 3  # the newest message with pictures and up to two before it keep their pictures; older ones become a note

    def _shown_pictures(self, rows: list[dict]) -> dict[int, list[dict]]:
        """Which pictures go to the model: from the newest message back, at most MODEL_PICTURE_MESSAGES messages and 4 pictures in all."""
        budget, windows, shown = images.MAX_PER_MESSAGE, self.MODEL_PICTURE_MESSAGES, {}
        for m in reversed(rows):
            if m["role"] != "user" or not (m["meta"] or {}).get("attachments"):
                continue
            if windows == 0 or budget == 0:
                break
            have = [a for a in (self.db.get_attachment(x["id"]) for x in m["meta"]["attachments"]) if a]
            shown[m["id"]] = have[:budget]
            budget -= len(shown[m["id"]])
            windows -= 1
        return shown

    def _user_content(self, m: dict, pictures: list[dict]):
        """A user message as the model gets it: plain text, or text plus image_url blocks (images only ever go in user messages)."""
        text = "(the owner sent only a picture)" if m["content"] == images.IMAGE_ONLY else m["content"]
        blocks, missing = [], len(m["meta"]["attachments"])
        for a in pictures:
            try:
                blocks.append({"type": "image_url", "image_url": {"url": images.for_model(Path(a["path"]).read_bytes(), a["mime"])}})
                missing -= 1
            except Exception:
                pass  # the file is gone: it counts as one that was not shown
        text += " [an image was shared here]" * missing  # the pictures that are not sent (older than the window, or no longer on disk) leave a trace in the text
        text += f"\n[attached pictures, ids: {', '.join(x['id'] for x in m['meta']['attachments'])}]"  # so a task can ask for one by id (mobile_task files)
        return [{"type": "text", "text": text}, *blocks] if blocks else text

    def _history(self, bot_id: str) -> list[dict]:
        out: list[dict] = []
        rows = self.db.messages(bot_id, 30)
        shown = self._shown_pictures(rows)
        for m in rows:
            if m["kind"] in ("text", "result"):
                if m["role"] == "user" and (m["meta"] or {}).get("attachments"):
                    out.append({"role": "user", "content": self._user_content(m, shown.get(m["id"], []))})
                    continue
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
                         "so find_app tells you what is installed. NEVER offer, ask about or promise a Play Store install (this phone has none): if an app is not installed, say it needs an APK "
                         "from the owner or another phone (name which one from the phone directory). Games draw one picture and have no UI tree: the worker plays from "
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
        parts.append(phones.render(phones.describe(self.db, self.jobs), phones.phone_key(bot), master=bot["kind"] == "master"))
        lane = self.jobs.lane_of(bot_id)
        busy_with = [j for j in self.db.active_jobs() if j["bot_id"] != bot_id and self.jobs.lane_of(j["bot_id"]) == lane]
        if busy_with:
            parts.append(f"Other bots have {len(busy_with)} task(s) on the same phone; this bot's tasks queue behind them." if lane else
                         f"Other bots have {len(busy_with)} task(s) on the shared phone; this bot's tasks queue behind them.")
        return "\n\n".join(parts)

    def _decide(self, model, system: str, msgs: list[dict]) -> dict | None:
        for attempt in range(2):
            try:
                raw = model.chat(system, msgs)
            except ModelError as e:
                if "HTTP 400" in str(e) and any(isinstance(m.get("content"), list) for m in msgs):  # the provider refused a picture (corrupt, unsupported, too large for the request)
                    return {"type": "reply", "text": "I couldn't read that picture, so I can't tell what it shows. Try sending it again, a different screenshot, or describe it in words."}
                raise
            try:
                return parse_decision(raw)
            except (ValueError, json.JSONDecodeError):
                if "{" not in raw and raw.strip():  # plain prose, no JSON at all: it is the answer, so show it instead of asking again (a second ask can come back as "understood")
                    return {"type": "reply", "text": raw.strip()}
                msgs = msgs + [{"role": "assistant", "content": raw},
                               {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object, for the same request as before, and do not mention this correction."}]
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
                self._reroutes[bot_id] = 0
                self._attempts.pop(bot_id, None)
                self._explain_only.discard(bot_id)
                self._summarize.discard(bot_id)
            if note is None and self._open_shortcut(bot_id):  # "open <an app known not to run on this phone>": no model needed, the chain goes straight to the phone that works
                return
            try:
                self._turn(bot_id, note)
            except Exception as e:  # keep the chat usable if the model or network fails
                self.post(bot_id, f"Sorry, I hit a problem and could not answer ({type(e).__name__}). Please try again.")
            finally:
                self._origin.pop(bot_id, None)

    def _open_shortcut(self, bot_id: str) -> bool:
        """A plain "open <app>" message for an app that is known not to run on this bot's phone is answered by the escalation chain at once (no question, no model turn)."""
        last = next((m for m in reversed(self.db.messages(bot_id, 4)) if m["kind"] in ("text", "result", "status", "action_item") or m["role"] == "user"), None)
        if not last or last["role"] != "user" or not SIMPLE_OPEN.match(last["content"]):
            return False
        key = phones.phone_key(self.db.get_bot(bot_id) or {})
        pkg = self.escalator.known_bad_package(key, last["content"])
        if not pkg:
            return False
        self._answered[bot_id] = self._latest_owner(bot_id)
        app = app_name(last["content"], pkg)
        self.escalator.start(bot_id, pkg, last["content"], key, f"{app} doesn't run on {self.escalator._label(key)} (it crashed there before), so I'm using another phone.")
        return True

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
            if kind in ("delegate_task", "mobile_task", "mobile_plan"):
                err = self._reroute_guard(bot_id, act, kind)
                if err:  # a reroute turn may not repeat what was tried or go past its limit: the model hears why and explains instead
                    msgs = msgs + [{"role": "assistant", "content": json.dumps({"action": act})}, {"role": "user", "content": f"[tool result for {kind}] {err}"}]
                    continue
            if kind == "delegate_task" and (self.db.get_bot(bot_id) or {}).get("kind") == "master":
                self._delegate(bot_id, act)
                return
            if kind == "move_app" and (self.db.get_bot(bot_id) or {}).get("kind") == "master":
                self._move_app(bot_id, act)
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
            if kind == "web_options":
                self.web_options(bot_id, str(act.get("query", "")), str(act.get("goal", "")))
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

    def _phone_key(self, bot_id: str, on: str) -> str:
        """Which phone `on` names: emulator, games, or phone:<id> of a real phone's bot ("" means the bot's own phone). 'error: ...' if unknown."""
        on = (on or "").strip().lower()
        own = self.db.get_bot(bot_id) or {}
        real = [b for b in self.db.list_bots() if b["kind"] == "phone"]
        if not on:
            return phones.phone_key(own)
        if on in ("emulator", "shared", "shared emulator", "main", "default"):
            return "emulator"
        if on in ("games", "game", "games phone", "virtual"):
            return "games"
        hit = next((b for b in real if b["name"].lower() == on or b["phone_id"].lower() == on), None) or next((b for b in real if b["name"].lower().startswith(on)), None)
        if not hit:
            return "error: unknown phone '" + on + "'; use emulator, games" + "".join(f", {b['name']}" for b in real)
        return phones.phone_key(hit)

    def _resolve_phone(self, bot_id: str, on: str) -> tuple | str:
        """(device, lifecycle, label) of the phone a check should read: the bot's own, or the one named in `on`. An error string if unknown or offline."""
        key = self._phone_key(bot_id, on)
        if key.startswith("error"):
            return key
        if key == "emulator":
            return self.jobs.device, self.jobs.lifecycle, "the shared emulator"
        if key.startswith("phone:"):
            b = next((x for x in self.db.list_bots() if x["kind"] == "phone" and x["phone_id"] == key.split(":", 1)[1]), {"name": "your phone"})
            pid = self.jobs.phones.pick(key.split(":", 1)[1]) if self.jobs.phones else None
            if not pid:
                return f"error: {b['name']} is offline (the Mobile Agent app is not connected), so I cannot look at it"
            return RemoteDevice(self.jobs.phones, pid), None, b["name"]
        dev, lc = self.jobs.virtuals.get(key, (None, None))
        if dev is None:
            return "error: the games phone is not set up on this computer"
        return dev, lc, "the games phone"

    def _phone_check(self, bot_id: str, act: dict) -> str:
        """Read a fact straight from a phone (app installed, Google account signed in, app in front) so a task's result can be checked, not trusted.
        `on` picks the phone (default: the bot's own), so a bot can see where an app is installed before it decides where to send a task."""
        target = self._resolve_phone(bot_id, str(act.get("on", "")))
        if isinstance(target, str):
            return target
        dev, lc, label = target
        if label == "the games phone" and act.get("kind") == "account":
            return "no (on the games phone): it has no Google services, so no Google account can be signed in there"
        if not hasattr(dev, "packages"):
            return "error: phone checks are not available"
        if lc is not None and not lc.ensure_healthy(90):
            return f"error: {label} is not available right now (it is starting). Start the task instead; it waits for the phone."
        ok, detail = run_check(dev, act)
        return f"{'yes' if ok else 'no'}{' (on ' + label + ')' if act.get('on') else ''}: {detail}"

    def _queue_tasks(self, bot_id: str, act: dict, kind: str) -> None:
        """mobile_task / mobile_plan: queue one phone task, or several in order. Repeats are ignored, a step that needs an earlier one waits for it."""
        raw = act.get("steps") if kind == "mobile_plan" else [act]
        steps = [st for st in (raw if isinstance(raw, list) else []) if isinstance(st, dict) and str(st.get("goal", "")).strip()][:MAX_PLAN_STEPS]
        if not steps:
            self.post(bot_id, "What should I do on the phone?")
            return
        if kind == "mobile_task" and len(steps) == 1 and bot_id not in self._finishing:  # an app known not to run on this phone goes straight to the phone that works
            key = phones.phone_key(self.db.get_bot(bot_id) or {})
            pkg = self.escalator.known_bad_package(key, str(steps[0]["goal"]))
            if pkg:
                app = app_name(str(steps[0]["goal"]), pkg)
                self.escalator.start(bot_id, pkg, str(steps[0]["goal"]), key, f"{app} doesn't run on {self.escalator._label(key)} (it crashed there before), so I'm using another phone.")
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
            cloud_game = gamepolicy.cloud_play_request(goal)
            if cloud_game:  # playing it through a cloud-gaming site is the same bot risk: no phone, no browser, no task
                self.post(bot_id, f"I won't play {cloud_game} through a browser or a cloud-gaming site either: its publisher bans accounts that use bots, however the game is run. "
                                  "I can open the site and tell you what it shows, or you can play it yourself (Take control on the games phone).")
                return
            dup = next((j for j in known if same_goal(j["goal"], goal)), None)
            if dup:
                chain = dup["id"] if dup["id"] else chain  # a later step still waits for the task that already exists
                continue
            if len(known) >= MAX_ACTIVE_PER_BOT:
                break
            files = self._owned_files(bot_id, st.get("files") if kind == "mobile_plan" else act.get("files"))
            if files and own.get("kind") == "phone":  # the app on a real phone has no file hand-off yet: say so, do not run the task without the picture
                self.post(bot_id, "Sending a picture to your own phone is not supported yet (the Mobile Agent app cannot receive files). "
                                  "I can do it on the shared emulator, or you can do the picture step on your phone yourself.")
                return
            new.append((goal, self._clean_verify(st.get("verify")), chain, files))
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
        for goal, verify, dep, files in new:
            ids.append(self.jobs.submit(bot_id, goal, ids[dep] if isinstance(dep, int) else dep, verify, self._origin.get(bot_id), files=files)["id"])
            self._attempts.setdefault(bot_id, []).append((bot_id, goal))

    def _owned_files(self, bot_id: str, ids) -> list[str]:
        """The pictures a task asks for, kept only if they were really sent in this bot's chat (a model cannot reach another bot's pictures)."""
        out = []
        for fid in (ids if isinstance(ids, list) else [])[:images.MAX_PER_MESSAGE]:
            row = self.db.get_attachment(str(fid))
            if row and row["bot_id"] == bot_id and row["message_id"] is not None and str(fid) not in out:
                out.append(str(fid))
        return out

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

    # --- other ways: research, then a card the owner answers ------------------------------------------------------------------------------------
    def web_options(self, bot_id: str, query: str, goal: str = "", intro: str = "") -> bool:
        """Search the web for other ways to do something, read what the sites say about themselves, and post a card with the options and their honest risks.
        Nothing is opened or done until the owner picks (web_pick). The search never carries the owner's data (websearch.clean_query)."""
        if not websearch.plan_queries(query, goal) or not self.db.get_bot(bot_id):
            self.post(bot_id, "Tell me a little more about what you want to do, so I know what to look for.")
            return False
        subject = websearch.clean_query(goal or query)
        game = (gamepolicy.mentions_blocked(f"{query} {goal}") or [""])[0]
        self.post(bot_id, f"Looking for other ways: {websearch.clean_query(query or goal)}", "bot_note", {"search": query})
        notes: list[str] = []
        try:
            found = options.research(query, goal, self.model_factory(ORCHESTRATOR_MODEL), self.web_provider, self.web_fetch, game, notes=notes)
        except Exception as e:
            self.post(bot_id, f"I could not search the web just now ({type(e).__name__}), so I have no options to show. Try again in a minute.")
            return False
        if not found:
            self.post(bot_id, "I looked and did not find a way that I can describe honestly. Nothing was opened or changed.")
            return False
        labels: dict[str, dict] = {}
        for o in found:
            label = o["name"][:70]
            while label in labels:
                label += "*"
            labels[label] = {k: o[k] for k in ("name", "url", "official", "what")}
        self.make_item(bot_id, None, {"kind": "choice", "question": options.card_text(found, intro or f"Other ways to do this, with what I could read about each ({subject[:60]}):", notes),
                                      "options": [*list(labels)[:options.MAX_SHOWN], options.NONE_LABEL], "web": {"stage": "option", "options": labels, "game": game}})
        return True

    def web_pick(self, item: dict, choice: str) -> None:
        """The owner answered a web_options card. The only things that can happen: nothing, the link shown, or the link opened on a phone with a handoff to them."""
        bot_id, web = item["bot_id"], item["payload"].get("web") or {}
        if choice == options.NONE_LABEL or choice == "Cancel":
            self.post(bot_id, "Okay. Nothing was opened or changed. Ask me again whenever you decide.")
            return
        if web.get("stage") == "option":
            o = web["options"].get(choice)
            if not o:
                return
            phones_ = ["Open it on the emulator"] + (["Open it on the games phone"] if "games" in self.jobs.virtuals else []) + \
                      [f"Open it on {b['name']}" for b in self.db.list_bots() if b["kind"] == "phone" and self.jobs.phones and self.jobs.phones.online(b["phone_id"])]
            self.make_item(bot_id, None, {"kind": "choice", "question": f"{o['name']}{'' if options.domain(o['url']) in o['name'].lower() else ' (' + options.domain(o['url']) + ')'}: what should I do? Nothing runs until you pick.",
                                          "options": ["Show me the link", *phones_[:3], "Cancel"], "web": {"stage": "action", "option": o, "game": web.get("game", "")}})
            return
        o = web.get("option") or {}
        url = str(o.get("url", ""))
        if choice == "Show me the link":
            self.post(bot_id, f"{o.get('name')}: {url}\n{'Official.' if o.get('official') else 'Unofficial third party: read its terms yourself before you sign in or pay.'}")
            return
        if not choice.startswith("Open it on ") or not url.startswith("https://"):
            return
        target = choice.removeprefix("Open it on ")
        try:
            self._open_for_owner(bot_id, target, url)
        except Exception as e:
            self.post(bot_id, f"I could not open it on {target}: {type(e).__name__}: {str(e)[:120]}. Here is the link instead: {url}")
            return
        if not o.get("official"):
            gamepolicy.mark_third_party(options.domain(url))  # from now on nothing is typed on that site for the owner
        game = web.get("game", "")
        self.post(bot_id, f"Opened {o.get('name')} on {target}. I won't type, sign in or play on it" + (f": {game} is a game whose publisher bans bot accounts, and my guard stops me from playing it there" if game else "") +
                  ". Sign in yourself" + (" (use Take control in its Mobile panel)" if target in ("the emulator", "the games phone") else "") + ".")
        self.make_item(bot_id, None, {"kind": "handoff", "question": f"Sign in yourself on {target}, then tell me when you're done."})

    def _open_for_owner(self, bot_id: str, target: str, url: str) -> None:
        """Open a link in the browser of one of the owner's phones (the only thing a web_options pick can do)."""
        if target == "the emulator":
            dev, lc = self.jobs.device, self.jobs.lifecycle
        elif target == "the games phone":
            dev, lc = self.jobs.virtuals.get("games", (None, None))
        else:
            bot = next((b for b in self.db.list_bots() if b["kind"] == "phone" and b["name"] == target), None)
            if not bot or not self.jobs.phones or not self.jobs.phones.online(bot["phone_id"]):
                raise ConnectionError("that phone is not connected")
            dev, lc = RemoteDevice(self.jobs.phones, self.jobs.phones.pick(bot["phone_id"])), None
        if dev is None:
            raise ValueError("that phone is not set up")
        if lc is not None and not lc.ensure_healthy(90):
            raise TimeoutError("the phone did not wake up in time")
        dev.open_url(url)

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

    def _move_app(self, bot_id: str, act: dict) -> None:
        """Master: copy an installed app between the adb phones (shared emulator <-> games phone) without any store. It runs in the background with a progress
        message, then Master gets a follow-up turn to carry on (verify, open it on the other phone) or to explain."""
        name = str(act.get("app") or act.get("package") or "").strip()
        if not name:
            self.post(bot_id, "Which app should I move?")
            return
        src_on, dst_on = str(act.get("from") or "emulator"), str(act.get("to") or "games")
        keys = [self._phone_key(bot_id, src_on), self._phone_key(bot_id, dst_on)]
        bad = next((k for k in keys if k.startswith("error")), None)
        if bad:
            self.post(bot_id, bad.replace("error: ", "I can't move it: "))
            return
        if any(k.startswith("phone:") for k in keys):  # a real phone installs from its own Play Store; copying APKs over a remote link is not offered
            self.post(bot_id, "I can't move apps to or from your own phone: it installs from its own Play Store, so ask its bot to install it there as a normal task.")
            return
        if keys[0] == keys[1]:
            self.post(bot_id, "That is the same phone twice: pick two different phones to move an app between.")
            return
        src, dst = self._resolve_phone(bot_id, src_on), self._resolve_phone(bot_id, dst_on)
        if isinstance(src, str) or isinstance(dst, str):
            self.post(bot_id, (src if isinstance(src, str) else dst).replace("error: ", "I can't move it: "))
            return
        if not all(hasattr(d, "apk_paths") for d in (src[0], dst[0])):
            self.post(bot_id, "I can't move apps between those phones: they do not expose their app files.")
            return
        self.post(bot_id, str(act.get("say", "")).strip() or f"Moving {name} from {src[2]} to {dst[2]}…")
        threading.Thread(target=self._do_move, args=(bot_id, name, str(act.get("package") or ""), src, dst), daemon=True, name="move-app").start()

    def _do_move(self, bot_id: str, name: str, package: str, src: tuple, dst: tuple) -> None:
        (sdev, slc, slabel), (ddev, dlc, dlabel) = src, dst
        try:
            for lc, label in ((slc, slabel), (dlc, dlabel)):
                if lc is not None and not lc.ensure_healthy(90):
                    raise ValueError(f"{label} is not available right now (it is still starting)")
            pkg = package
            if not pkg:
                hits = find_packages(sdev.packages(), name)
                if not hits:
                    raise ValueError(f"no installed app matches '{name}' on {slabel}")
                if len(hits) > 1:
                    raise ValueError(f"'{name}' matches several apps on {slabel} ({', '.join(hits[:4])}): give the exact package")
                pkg = hits[0]
            detail = move_app(sdev, ddev, pkg)
            ok, _ = run_check(ddev, {"kind": "app_installed", "query": pkg})
            if not ok:
                raise ValueError(f"{pkg} is not listed on {dlabel} after the move")
            self._summarize.add(bot_id)
            text = f"Moved {name} from {slabel} to {dlabel} ({detail}). Only the app was copied, not its data or its sign-in."
            note = (f"move_app finished: {text} Next, if the owner wanted it used there, hand a short task to {dlabel}'s bot with delegate_task (open the app and report what you see; "
                    "do not play unless they asked), or run it yourself if that phone is the emulator. Otherwise tell the owner in one sentence.")
        except Exception as e:
            reason = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {str(e)[:150]}"
            text = f"I could not move {name} from {slabel} to {dlabel}: {reason}."
            note = (f"move_app failed: {text} Tell the owner plainly what stopped it and offer only options that really exist (for example installing from the Play Store on the "
                    "emulator first, or an APK for the games phone). Do not promise to retry.")
        self.post(bot_id, text)
        self._turn_with(bot_id, note, "The move is finished; tell me what to do next.")

    def _reroute_guard(self, bot_id: str, act: dict, kind: str) -> str | None:
        """While Master reacts to a task that did not complete, it may not start the same goal on a phone that already tried it, nor go past MAX_REROUTES."""
        if bot_id not in self._finishing or (self.db.get_bot(bot_id) or {}).get("kind") != "master":
            return None
        if bot_id in self._explain_only:
            return "error: you already rerouted this request twice. Start nothing else: tell the owner what was tried where, why it cannot work, and what would unblock it."
        goals = [str(st.get("goal", "")) for st in (act.get("steps") or [])] if kind == "mobile_plan" else [str(act.get("goal", ""))]
        if kind == "delegate_task":
            target = self._find_bot(str(act.get("bot", "")), (bot_id,))
            tid = target["id"] if isinstance(target, dict) else None
        else:
            tid = bot_id  # Master's own tasks run on the shared emulator
        tried = self._attempts.get(bot_id, [])
        if tid and any(t == tid and same_goal(g, goal) for t, g in tried for goal in goals if goal):
            name = (self.db.get_bot(tid) or {}).get("name", "that phone")
            return f"error: {name} already tried this. Choose a different phone from the directory, or explain to the owner."
        return None

    def _reroute_note(self, master_id: str, bot_name: str, job: dict) -> str:
        """What Master is told when a task did not complete: find another phone that can really do it (at most MAX_REROUTES times), then report honestly."""
        n = self._reroutes.get(master_id, 0)
        self._reroutes[master_id] = n + 1
        self._summarize.add(master_id)
        head = (f"The task for {bot_name} did not complete ({job['status']}). Goal: {job['goal']}. Worker report: {job['result'] or '(none)'}.\n")
        if n >= MAX_REROUTES:
            self._explain_only.add(master_id)
            return head + ("You already rerouted this request twice, so do NOT start anything else (it would be refused). Reply to the owner in one or two short sentences: "
                           "what was tried on which phone, why it cannot work, and the one thing that would unblock it. Be honest.")
        return head + ("Read the phone directory. If a different phone could really do this (the app is installed there, per phone_check with \"on\", or it is a Play Store install and "
                       "the emulator can do it; never a phone already tried for this goal or listed as Known NOT to run it), start it there: delegate_task to that phone's bot, or "
                       "mobile_task for the emulator, and tell the owner in one sentence that you moved it and why. An app that crashed on a phone is NOT yours to move: the system already tries the "
                       "owner's other phones in order and posts the result itself. If no phone can, reply in one or two short sentences: what was tried "
                       "on which phone, why it cannot work, and the one thing that would unblock it (an APK on the games phone, the Mobile Agent app connected on their own phone). "
                       "Be honest and do not promise anything you are not doing right now.")

    def _delegate(self, master_id: str, act: dict) -> None:
        """The master hands a phone task to the bot that fits it. The task runs, and is reported, in that bot's chat; the master hears back too."""
        goal, say = str(act.get("goal", "")).strip(), str(act.get("say", "")).strip()
        if not goal:
            self.post(master_id, "What should I get done on the phone?")
            return
        cloud_game = gamepolicy.cloud_play_request(goal)
        if cloud_game:
            self.post(master_id, f"I won't play {cloud_game} through a browser or a cloud-gaming site either: its publisher bans accounts that use bots, however the game is run. "
                                 "I can open the site and tell you what it shows, or you can play it yourself (Take control on the games phone).")
            return
        target = self._find_bot(str(act.get("bot", "")), (master_id,))
        if isinstance(target, str):
            self.post(master_id, target.replace("that you can ask now", "to hand this to") + ". I can run it myself instead if you like.")
            return
        tkey = phones.phone_key(target)
        pkg = self.escalator.known_bad_package(tkey, goal)
        if pkg:  # it is known not to run on that bot's phone: do not send it there, try the others
            app = app_name(goal, pkg)
            self.escalator.start(master_id, pkg, goal, tkey, f"{app} doesn't run on {self.escalator._label(tkey)} (it crashed there before), so I'm using another phone.")
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
        self._attempts.setdefault(master_id, []).append((target["id"], goal))

    @staticmethod
    def _policy_note(job: dict) -> str:
        return (f"The phone job was stopped by the game-account guard. Goal: {job['goal'][:200]}. Report: {job['result']}\n"
                "Tell the owner in two short sentences: you did not play it because the game's publisher bans accounts that use bots; what you CAN do (open it, check it is installed, "
                "move it between phones, tell what the screen shows) and that they can play it themselves with Take control on the phone it is on. Do not retry, do not try another phone, "
                "do not offer to play it another way.")

    def job_finished(self, bot_id: str, job: dict) -> None:
        if self.escalator.owns(job["id"]):  # a task the escalation chain started on the owner's own phone: the chain decides what comes next
            self.escalator.job_done(job)
            return
        master_id = self.delegated.pop(job["id"], None)
        crashed = job["status"] == "failed" and job["result"].startswith(CRASH_PREFIX)
        policy = job["status"] == "denied" and job["result"].startswith(gamepolicy.POLICY_PREFIXES)  # the game guard refused to play (or to type on a third-party site)
        if master_id:
            bot = self.db.get_bot(bot_id) or {"name": "A bot"}
            text = {"done": f"{bot['name']} finished: {job['goal']}\n{job['result']}", "cancelled": f"{bot['name']}'s task was stopped: {job['goal']}"}.get(
                job["status"], f"{bot['name']}'s task did not complete ({job['status']}): {job['goal']}\n{job['result']}")
            self.post(master_id, text, "bot_note", {"with": bot_id})
            if job["status"] == "done" and self.db.get_bot(master_id):  # the owner asked Master, so Master gives the answer in its own chat (not only a raw note from the other bot)
                self._summarize.discard(master_id)
                bot_name = (self.db.get_bot(bot_id) or {"name": "a bot"})["name"]
                note = (f"The task you handed to {bot_name} finished. Goal: {job['goal']}. Worker report: {job['result']}\nTell the owner the final result in one or two short sentences: "
                        "what ran on which phone and what the screen showed. Start nothing else unless the owner asked for it. Be honest and do not claim more than the report shows.")
                threading.Thread(target=self._turn_with, args=(master_id, note, f"{bot_name} finished: {job['result'][:200]}"), daemon=True, name="summary").start()
            if policy and self.db.get_bot(master_id):
                bot_name = (self.db.get_bot(bot_id) or {"name": "a bot"})["name"]
                threading.Thread(target=self._turn_with, args=(master_id, self._policy_note(job), f"{job['result']}"), daemon=True, name="policy").start()
            if job["status"] in ("failed", "max_steps") and not crashed and self.db.get_bot(master_id):  # Master looks for another way, without blocking this phone's lane
                bot_name = (self.db.get_bot(bot_id) or {"name": "a bot"})["name"]
                threading.Thread(target=self._turn_with, args=(master_id, self._reroute_note(master_id, bot_name, job), f"{bot_name}'s task did not complete: {job['result'] or job['goal']}"),
                                 daemon=True, name="reroute").start()
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
        if policy:  # the game guard stopped it: no retry, no other phone, just an honest answer with what is still possible
            self._retries[bot_id] = MAX_AUTO_RETRIES
            self.db.memory_add(bot_id, f"Not played (game account protection): {job['goal'][:120]}")
            if not master_id:
                self._turn_with(bot_id, self._policy_note(job), job["result"], job.get("routine"))
            return
        if crashed:  # retrying a crashing app only repeats the crash: remember it and try the owner's other phones in order, without asking
            self._retries[bot_id] = MAX_AUTO_RETRIES
            self.db.memory_add(bot_id, f"Task failed: {job['goal']} -> {result}")
            m = phones.CRASH_RE.match(result)
            if m:
                asker, failed = master_id or bot_id, phones.job_phone_key(job)
                if self.db.get_bot(asker):
                    app = app_name(job["goal"], m.group(1))
                    self.escalator.start(asker, m.group(1), job["goal"], failed, f"{app} crashed on {self.escalator._label(failed)}, so I'm trying your other phones.")
                    return
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
        own = self.db.get_bot(bot_id) or {}
        if status in ("failed", "max_steps") and own.get("kind") == "master" and not master_id:  # Master's own task on the emulator: look for another phone too
            note += " " + self._reroute_note(bot_id, "yourself (the shared emulator)", job)
        elif status in ("failed", "max_steps"):
            note += (" If your phone could not do this but another phone in the directory could, say which one and tell the owner to ask Master (it can hand a task to any phone). "
                     "Never offer or promise an install or action your own phone cannot do, and do not start anything you are not doing right now.")
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
