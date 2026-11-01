"""Chat-first API: bots, messages, live events, action cards, library, and the shared phone."""
import asyncio
import json
import re
import shutil
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent import images, sensitive
from agent.config import TRANSCRIBE_MODEL


MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 10 minutes of 16 kHz mono 16-bit audio is about 19 MB


class NewBot(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    persona: str = Field(default="", max_length=4000)
    greet: bool = False  # the bot opens the conversation and asks what to do first


class BotPatch(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    persona: str | None = Field(default=None, max_length=4000)


class NewMessage(BaseModel):
    text: str = Field(default="", max_length=4000)
    attachments: list[str] = Field(default_factory=list, max_length=images.MAX_PER_MESSAGE)  # ids of pictures uploaded to this bot (POST /attachments), not sent yet


class Answer(BaseModel):
    approved: bool | None = None
    value: str | None = Field(default=None, max_length=500)
    choice: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=12)
    secret: str | None = Field(default=None, max_length=2000, repr=False)
    label: str | None = Field(default=None, max_length=60)
    done: bool | None = None


class RoutinePatch(BaseModel):
    enabled: bool


class Control(BaseModel):
    action: str = Field(pattern="^(take|release)$")


def register_chat(app: FastAPI, auth, db, hub, jobs, orch, vault, scheduler) -> None:
    def bot_or_404(bot_id: str) -> dict:
        b = db.get_bot(bot_id)
        if not b:
            raise HTTPException(404, "unknown bot")
        return b

    @app.get("/api/bots", dependencies=[Depends(auth)])
    def list_bots():
        sync = getattr(app.state, "sync_adb", None)
        if sync and getattr(jobs, "adb_phones", None) is not None:  # extra adb phones: look again, in the background, so the list never waits for adb
            threading.Thread(target=sync, daemon=True, name="adb-sync").start()
        bots = db.list_bots()
        for b in bots:
            a = jobs.active_for(b["id"])
            b["active"] = a["status"] if a else None
        return bots

    @app.post("/api/bots", dependencies=[Depends(auth)])
    def create_bot(body: NewBot):
        bot = db.create_bot(body.name, body.persona)
        if body.greet:
            focus = f"The owner described its focus as: {bot['persona']}" if bot["persona"] else "The owner gave it no particular focus."
            note = (f"You were just created as the bot '{bot['name']}'. {focus} Open the conversation yourself: in two or three short sentences say what you can do "
                    "for them given that focus (only things you can really do: chat, remember things, run recurring routines, use the phone, ask other bots), then ask ONE concrete "
                    "question about what they want to start with, with two or three example first tasks that fit the focus. Do not start a phone task or set a routine yet.")
            threading.Thread(target=orch.turn, args=(bot["id"], note), daemon=True).start()
        return bot

    @app.patch("/api/bots/{bot_id}", dependencies=[Depends(auth)])
    def rename_bot(bot_id: str, body: BotPatch):
        bot_or_404(bot_id)
        if not body.name.strip():
            raise HTTPException(422, "name is required")
        return db.rename_bot(bot_id, body.name, body.persona)

    @app.get("/api/bots/{bot_id}/delete-preview", dependencies=[Depends(auth)])
    def delete_preview(bot_id: str):
        """What deleting this bot removes and what stays, so the owner can decide with the facts in front of them."""
        bot_or_404(bot_id)
        return {**db.bot_counts(bot_id), "secrets": len(vault.entries()), "last": len([b for b in db.list_bots() if b["kind"] != "master"]) < 2,
                "running": bool(jobs.active_for(bot_id))}

    @app.delete("/api/bots/{bot_id}", dependencies=[Depends(auth)])
    def delete_bot(bot_id: str, erase_secrets: bool = Query(default=False)):
        """Removes the bot's chat, tasks (with their screenshots and traces), routines, memory and library. Saved secrets belong to the owner and are
        shared by all bots, so they stay, unless this is the last bot and the owner asks to erase them too. Deleting the last bot leaves a fresh Assistant."""
        if bot_or_404(bot_id)["kind"] == "master":
            raise HTTPException(409, "The master bot cannot be deleted")
        last = len([b for b in db.list_bots() if b["kind"] != "master"]) < 2
        if erase_secrets and not last:
            raise HTTPException(409, "Secrets are shared with your other bots; erase them when deleting the last bot")
        active = jobs.active_for(bot_id)
        if active:  # stop the phone task and let it finish before its data is removed
            jobs.cancel(active["id"])
            for _ in range(60):
                if not jobs.active_for(bot_id) and not any(r.bot_id == bot_id for r in jobs.running()):
                    break
                time.sleep(0.25)
        job_ids = db.job_ids(bot_id)
        for path in db.delete_bot(bot_id):
            Path(path).unlink(missing_ok=True)
        for jid in job_ids:  # screenshots and prompts of its tasks
            shutil.rmtree(Path(jobs.data_dir) / "runs" / jid, ignore_errors=True)
        erased = vault.erase_all() if erase_secrets else 0
        fresh = db.create_bot("Assistant", "") if last else None
        return {"ok": True, "bot": fresh, "erased_secrets": erased}

    def own_lane(bot_id: str) -> str:
        """The Mobile panel of the games phone's bot shows that phone; every other bot shows the shared emulator (a real phone's panel uses the phone list)."""
        lane = jobs.lane_of(bot_id)
        return lane if lane.startswith("virtual:") else ""

    @app.get("/api/bots/{bot_id}/state", dependencies=[Depends(auth)])
    def bot_state(bot_id: str):
        b = bot_or_404(bot_id)
        return {"bot": b, "messages": db.messages(bot_id, 200), "last_seq": hub.last_seq(bot_id),
                "jobs": db.jobs(bot_id, 10), "mobile": jobs.state(own_lane(bot_id))}

    uploads = Path(jobs.data_dir) / "uploads"
    upload_times: dict[str, deque] = {}

    def drop_attachment(row: dict) -> None:
        Path(row["path"]).unlink(missing_ok=True)
        db.delete_attachment(row["id"])

    @app.post("/api/bots/{bot_id}/attachments", dependencies=[Depends(auth)])
    async def upload_attachment(bot_id: str, request: Request):
        """One picture per request (the body is the picture itself, any image content type; the file name is never trusted). It is checked by decoding it,
        shrunk to 1568 px, stripped of all metadata (EXIF, GPS, camera) and stored; the answer gives its id, to send with the next message."""
        bot_or_404(bot_id)
        now = time.time()
        recent = upload_times.setdefault(bot_id, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= 20:
            raise HTTPException(429, "Too many pictures in a minute. Wait a moment and try again.")
        recent.append(now)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > images.MAX_UPLOAD_BYTES + 4096:
            raise HTTPException(413, f"That picture is too big. The limit is {images.MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > images.MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"That picture is too big. The limit is {images.MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
        for row in db.stale_attachments(3600):  # pictures that were picked but never sent
            drop_attachment(row)
        if db.unattached_count(bot_id) >= 2 * images.MAX_PER_MESSAGE:
            raise HTTPException(409, "Too many pictures are waiting to be sent. Send them or remove some first.")
        try:
            out, mime, w, h = await asyncio.get_running_loop().run_in_executor(None, images.normalize, bytes(data))
        except images.ImageError as e:
            raise HTTPException(400, str(e)) from e
        aid = uuid.uuid4().hex[:12]
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / f"{aid}.{images.EXT[mime]}"
        path.write_bytes(out)
        row = db.add_attachment(bot_id, mime, str(path), w, h, len(out), aid)
        return {"id": row["id"], "w": w, "h": h, "mime": mime, "bytes": len(out)}

    @app.get("/api/attachments/{attachment_id}/file", dependencies=[Depends(auth)])
    def attachment_file(attachment_id: str):
        row = db.get_attachment(attachment_id)
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404, "unknown picture")
        return FileResponse(row["path"], media_type=row["mime"], headers={"Cache-Control": "private, max-age=3600"})

    @app.delete("/api/attachments/{attachment_id}", dependencies=[Depends(auth)])
    def delete_attachment(attachment_id: str):
        """Take back a picture that was picked but not sent."""
        row = db.get_attachment(attachment_id)
        if not row:
            raise HTTPException(404, "unknown picture")
        if row["message_id"] is not None:
            raise HTTPException(409, "That picture was already sent")
        drop_attachment(row)
        return {"ok": True}

    @app.post("/api/bots/{bot_id}/messages", dependencies=[Depends(auth)])
    def post_message(bot_id: str, body: NewMessage):
        bot_or_404(bot_id)
        ids = list(dict.fromkeys(body.attachments))
        if not body.text.strip() and not ids:
            raise HTTPException(422, "Write a message or attach a picture")
        pics = db.unattached(bot_id, ids)
        if len(pics) != len(ids):  # another bot's upload, one already sent, or one that no longer exists
            raise HTTPException(422, "One of the pictures is not available. Attach it again.")
        text, kinds = sensitive.redact(body.text.strip())  # secrets never reach the log or a model
        meta = {"sensitive": kinds} if kinds else {}
        if pics:
            meta["attachments"] = [{"id": a["id"], "w": a["w"], "h": a["h"]} for a in pics]
        first_picture = bool(pics) and not db.attachment_notice_sent(bot_id)
        if first_picture:  # said once per chat, before the picture goes anywhere
            orch.post(bot_id, "Pictures you attach are sent to the model provider, the same as your messages. Avoid photos of cards, IDs or other private documents.",
                      "bot_note", {"notice": "image-privacy"})
        msg = db.add_message(bot_id, "user", "text", text or images.IMAGE_ONLY, meta or None)
        db.attach([a["id"] for a in pics], msg["id"])
        hub.publish(bot_id, {"type": "message", "message": msg})
        if kinds:  # offer a secure input for what was hidden, so the owner never has to paste it in chat again
            sk = {"card number": "card", "ID number": "id", "password": "password"}.get(kinds[0], "note")
            orch.make_item(bot_id, None, {"kind": "secret", "secret_kind": sk, "question": f"I hid a {', '.join(kinds)} from your message. To let me use it, "
                                          "enter it here with a name. It is encrypted, never shown again, and I only type it after you approve each use."})
        threading.Thread(target=orch.turn, args=(bot_id,), daemon=True).start()
        return msg

    @app.get("/api/bots/{bot_id}/events", dependencies=[Depends(auth)])
    async def events(bot_id: str, after: int = Query(default=-1)):
        bot_or_404(bot_id)

        async def gen():
            cursor, idle = after, 0
            while True:
                batch = hub.since(bot_id, cursor)
                for ev in batch:
                    cursor = ev["seq"]
                    yield f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n"
                idle = 0 if batch else idle + 1
                if idle and idle % 40 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/items/{item_id}/answer", dependencies=[Depends(auth)])
    def answer_item(item_id: str, body: Answer):
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "unknown item")
        if item["status"] != "open":
            raise HTTPException(409, "already answered")
        kind, payload = item["kind"], item["payload"]
        field = payload.get("field")
        if kind == "confirm":
            if body.approved is None:
                raise HTTPException(422, "approved is required")
            label = "Approved" if body.approved else "Declined"
            resume = {"value": "yes" if body.approved else "no"} if field else {"approved": body.approved}
            user_text = "Yes" if body.approved else "No"
        elif kind == "choice":
            if body.choice not in payload.get("options", []):
                raise HTTPException(422, "choice must be one of the options")
            label = user_text = body.choice
            resume = {"value": body.choice}
        elif kind == "text":
            text = (body.value or "").strip()
            if not text:
                raise HTTPException(422, "value is required")
            if sensitive.find(text):
                raise HTTPException(422, "that looks like a secret; add it from the Vault panel instead")
            label = user_text = text
            resume = {"value": text}
        elif kind == "handoff":
            label, resume, user_text = "Done", {"done": True}, "Done"
        elif kind == "secret":  # the value goes straight into the vault: never stored on the item, echoed or logged
            if body.approved is False:
                label, resume = "Declined", {"saved": ""}
            else:
                name = (payload.get("label") or body.label or "").strip()
                if not name or not body.secret:
                    raise HTTPException(422, "a name and a value are required")
                try:
                    rec = vault.put(name, body.secret, payload.get("secret_kind") or "password")
                except ValueError as e:
                    raise HTTPException(409, str(e)) from None
                label, resume = f"Saved as '{rec['label']}'", {"saved": rec["label"]}
            user_text = None
        else:  # otp: the code goes to the phone and is never stored, echoed or logged
            if body.approved is False:
                label, resume = "Declined", {"code": ""}
            elif body.code and re.fullmatch(r"\d{4,8}", body.code):
                label, resume = "Code sent to the phone", {"code": body.code}
            else:
                raise HTTPException(422, "code must be 4-8 digits")
            user_text = None

        if item["job_id"] and not jobs.resume(item["job_id"], resume):
            raise HTTPException(409, "the task is no longer waiting for this")
        db.close_item(item_id, "answered", None if kind in ("otp", "secret") else label)
        if item["message_id"]:
            m = db.get_message(item["message_id"])
            upd = db.update_message(item["message_id"], meta={**m["meta"], "status": "answered", "answer": label})
            hub.publish(item["bot_id"], {"type": "message", "message": upd})
        if field and kind in ("text", "choice"):
            db.memory_add(item["bot_id"], f"Owner said {field}: {resume['value']}")
        if not item["job_id"] and kind == "secret" and resume["saved"]:
            threading.Thread(target=orch.turn, args=(item["bot_id"], f"The owner saved the secret '{resume['saved']}' in the vault. Continue with what they wanted."), daemon=True).start()
        if not item["job_id"] and user_text:
            msg = db.add_message(item["bot_id"], "user", "text", user_text)
            hub.publish(item["bot_id"], {"type": "message", "message": msg})
            if payload.get("web"):  # a "other ways" card: the pick is carried out in code (open a link, show it, or nothing), never decided by a model turn
                threading.Thread(target=orch.web_pick, args=(item, user_text), daemon=True).start()
            else:
                threading.Thread(target=orch.turn, args=(item["bot_id"],), daemon=True).start()
        return {"ok": True}

    @app.get("/api/bots/{bot_id}/jobs", dependencies=[Depends(auth)])
    def bot_jobs(bot_id: str):
        bot_or_404(bot_id)
        return db.jobs(bot_id, 20)

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    def cancel_job(job_id: str):
        if not jobs.cancel(job_id):
            raise HTTPException(404, "job is not running")
        return {"ok": True}

    @app.get("/api/bots/{bot_id}/library", dependencies=[Depends(auth)])
    def library(bot_id: str):
        bot_or_404(bot_id)
        return db.library(bot_id)

    @app.get("/api/library/{lib_id}/file", dependencies=[Depends(auth)])
    def library_file(lib_id: str):
        item = db.get_library(lib_id)
        if not item:
            raise HTTPException(404, "unknown item")
        return FileResponse(item["path"], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    def phone_lane(phone: str | None) -> str:
        """`?phone=games` selects the games phone; no value is the shared emulator."""
        if not phone:
            return ""
        lane = f"virtual:{phone}"
        if lane not in {f"virtual:{n}" for n in jobs.virtuals}:
            raise HTTPException(404, "unknown phone")
        return lane

    @app.get("/api/mobile/state", dependencies=[Depends(auth)])
    def mobile_state(phone: str | None = Query(default=None)):
        return jobs.state(phone_lane(phone))

    @app.post("/api/mobile/wake", dependencies=[Depends(auth)])
    def mobile_wake(phone: str | None = Query(default=None)):
        lane = phone_lane(phone)
        lifecycle = jobs.pair(lane)[1]
        if lifecycle:
            lifecycle.wake()
        return jobs.state(lane)

    @app.post("/api/mobile/sleep", dependencies=[Depends(auth)])
    def mobile_sleep(phone: str | None = Query(default=None)):
        lane = phone_lane(phone)
        lifecycle = jobs.pair(lane)[1]
        if not lifecycle:
            raise HTTPException(409, "This phone cannot sleep")
        if not lifecycle.sleep():
            raise HTTPException(409, "The phone is busy or already asleep")
        return jobs.state(lane)

    @app.post("/api/mobile/control", dependencies=[Depends(auth)])
    def mobile_control(body: Control, phone: str | None = Query(default=None)):
        lane = phone_lane(phone)  # the shared emulator, or an owned virtual or adb phone by name
        jobs.take_control(lane) if body.action == "take" else jobs.release_control(lane)
        return jobs.state(lane)

    @app.get("/api/vault", dependencies=[Depends(auth)])
    def vault_list():
        return vault.entries()

    @app.delete("/api/vault/{secret_id}", dependencies=[Depends(auth)])
    def vault_delete(secret_id: str):
        if not vault.delete(secret_id):
            raise HTTPException(404, "unknown secret")
        return {"ok": True}

    @app.get("/api/vault/audit", dependencies=[Depends(auth)])
    def vault_audit():
        return vault.audit()

    @app.get("/api/bots/{bot_id}/routines", dependencies=[Depends(auth)])
    def routines_list(bot_id: str):
        bot_or_404(bot_id)
        return db.routines(bot_id)

    def routine_or_404(rid: str) -> dict:
        r = db.get_routine(rid)
        if not r:
            raise HTTPException(404, "unknown routine")
        return r

    @app.patch("/api/routines/{rid}", dependencies=[Depends(auth)])
    def routines_patch(rid: str, body: RoutinePatch):
        r = routine_or_404(rid)
        from agent.routines import next_run
        out = db.routine_update(rid, enabled=int(body.enabled), next_run=next_run(r["schedule"], scheduler.clock()))
        scheduler.on_change(r["bot_id"])
        return out

    @app.post("/api/routines/{rid}/run", dependencies=[Depends(auth)])
    def routines_run(rid: str):
        r = routine_or_404(rid)
        if scheduler.busy(r):
            raise HTTPException(409, "The last run of this routine is still working.")
        threading.Thread(target=scheduler.fire, args=(r, True), kwargs={}, daemon=True).start()
        return {"ok": True}

    @app.delete("/api/routines/{rid}", dependencies=[Depends(auth)])
    def routines_delete(rid: str):
        r = routine_or_404(rid)
        db.routine_delete(rid)
        scheduler.on_change(r["bot_id"])
        return {"ok": True}

    @app.post("/api/transcribe", dependencies=[Depends(auth)])
    async def transcribe(request: Request):
        """Speech to text for the chat microphone. The page sends a mono 16-bit WAV (16 kHz) as the raw body; the text goes back to the page so the
        owner can read and edit it before sending, so nothing is stored here."""
        wav = await request.body()
        if len(wav) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "That recording is too long (limit 10 minutes)")
        if len(wav) < 1000 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            raise HTTPException(422, "Send a WAV recording")
        try:
            text = await asyncio.get_running_loop().run_in_executor(None, lambda: orch.model_factory(TRANSCRIBE_MODEL).transcribe(wav))
        except Exception as e:
            raise HTTPException(502, f"Transcription failed ({type(e).__name__}). Please try again.") from None
        return {"text": text}
