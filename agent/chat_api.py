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

from agent import apkcheck, images, logs, sensitive
from agent.logs import audit
from agent.config import TRANSCRIBE_MODEL
from agent.connectors import ConnectorError
from agent.master import DEFAULT_ASSISTANT_PERSONA


MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 10 minutes of 16 kHz mono 16-bit audio is about 19 MB


class Connect(BaseModel):
    id: str | None = Field(default=None, max_length=40)  # a catalog entry, or...
    custom: dict | None = None  # ...{name, url | command, auth: {kind, header | env}}
    secret: str = Field(default="", max_length=4000)  # the key or token: goes to the vault, never stored on the connector or echoed


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
    use_relay: bool = False  # a one-time code: send the one the owner's phone found (the code itself never travels here)


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

    def bot_listed(b: dict) -> bool:
        """The sidebar starts with the master bot alone. A phone's bot appears when its phone is working or connected (or once it has been used); the owner makes every other bot."""
        if b["kind"] == "phone":
            return bool(jobs.phones and jobs.phones.online(b["phone_id"])) or db.has_activity(b["id"])
        if b["kind"] == "virtual" and b["phone_id"] == "games":
            pair = (getattr(jobs, "virtuals", None) or {}).get("games")
            return bool(pair and pair[1].power in ("on", "waking")) or db.has_activity(b["id"])
        return True

    @app.get("/api/bots", dependencies=[Depends(auth)])
    def list_bots(all: bool = Query(default=False)):
        sync = getattr(app.state, "sync_adb", None)
        if sync and getattr(jobs, "adb_phones", None) is not None:  # extra adb phones: look again, in the background, so the list never waits for adb
            threading.Thread(target=sync, daemon=True, name="adb-sync").start()
        bots = [b for b in db.list_bots() if all or bot_listed(b)]
        for b in bots:
            a = jobs.active_for(b["id"])
            b["active"] = a["status"] if a else None
        return bots

    @app.post("/api/bots", dependencies=[Depends(auth)])
    def create_bot(body: NewBot):
        bot = db.create_bot(body.name, body.persona)
        audit("bot created", bot=bot["id"], name=body.name)
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

    def remove_bot_data(bot_id: str) -> None:
        """Stop the bot's running task, then remove its chat, tasks (with screenshots and traces), routines, memory and library."""
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
            Path(path).with_suffix(".md").unlink(missing_ok=True)  # a report is an HTML page with a Markdown twin
        for jid in job_ids:  # screenshots and prompts of its tasks
            shutil.rmtree(Path(jobs.data_dir) / "runs" / jid, ignore_errors=True)

    orch.remove_bot = remove_bot_data

    @app.delete("/api/bots/{bot_id}", dependencies=[Depends(auth)])
    def delete_bot(bot_id: str, erase_secrets: bool = Query(default=False)):
        """Removes the bot's chat, tasks (with their screenshots and traces), routines, memory and library. Saved secrets belong to the owner and are
        shared by all bots, so they stay, unless this is the last bot and the owner asks to erase them too. Deleting the last bot leaves a fresh Assistant."""
        if bot_or_404(bot_id)["kind"] == "master":
            raise HTTPException(409, "The master bot cannot be deleted")
        last = len([b for b in db.list_bots() if b["kind"] != "master"]) < 2
        if erase_secrets and not last:
            raise HTTPException(409, "Secrets are shared with your other bots; erase them when deleting the last bot")
        remove_bot_data(bot_id)
        if erase_secrets and orch.connectors:
            orch.connectors.erase_all()  # their keys are in the vault: they go with it
        if erase_secrets:
            db.knowledge.erase_all()  # the shared notes go too: this is the "wipe everything" path
        erased = vault.erase_all() if erase_secrets else 0
        fresh = db.create_bot("Assistant", DEFAULT_ASSISTANT_PERSONA) if last and not db.master_bot() else None  # with a master bot the list can be just that
        return {"ok": True, "bot": fresh, "erased_secrets": erased}

    def own_lane(bot_id: str) -> str:
        """The Mobile panel of Cloud phone 2's bot shows that phone; every other bot shows the shared emulator (a real phone's panel uses the phone list)."""
        lane = jobs.lane_of(bot_id)
        return lane if lane.startswith("virtual:") else ""

    @app.get("/api/bots/{bot_id}/state", dependencies=[Depends(auth)])
    def bot_state(bot_id: str):
        b = bot_or_404(bot_id)
        return {"bot": b, "messages": db.messages(bot_id, 200), "last_seq": hub.last_seq(bot_id),
                "jobs": db.jobs(bot_id, 10), "mobile": jobs.state(own_lane(bot_id))}

    @app.get("/api/bots/{bot_id}/messages", dependencies=[Depends(auth)])
    def earlier_messages(bot_id: str, before: int = Query(ge=0), limit: int = Query(default=100, ge=1, le=200)):
        bot_or_404(bot_id)
        return db.messages_before(bot_id, before, limit)

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

    @app.post("/api/bots/{bot_id}/apk", dependencies=[Depends(auth)])
    async def upload_apk(bot_id: str, request: Request, name: str = Query(default="app.apk", max_length=120)):
        """An APK to check (the body is the file). Only stored: it is read with the SDK's static tools and installed on a throwaway cloud phone, never run on this computer
        and never put on the owner's own phone. The name is only shown; the file is stored under a generated one."""
        bot_or_404(bot_id)
        now = time.time()
        recent = upload_times.setdefault("apk:" + bot_id, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= 5:
            raise HTTPException(429, "Too many files in a minute. Wait a moment and try again.")
        recent.append(now)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > apkcheck.MAX_APK_BYTES + 4096:
            raise HTTPException(413, f"That file is too big. The limit is {apkcheck.MAX_APK_BYTES // 1024 // 1024} MB.")
        for row in db.stale_attachments(3600):
            drop_attachment(row)
        if db.unattached_count(bot_id) >= 2 * images.MAX_PER_MESSAGE:
            raise HTTPException(409, "Too many files are waiting to be sent. Send them or remove some first.")
        aid = uuid.uuid4().hex[:12]
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / f"{aid}.apk"
        size = 0
        try:
            with open(path, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > apkcheck.MAX_APK_BYTES:
                        raise HTTPException(413, f"That file is too big. The limit is {apkcheck.MAX_APK_BYTES // 1024 // 1024} MB.")
                    f.write(chunk)
            try:
                apkcheck.looks_like_apk(path)
            except apkcheck.ApkError as e:
                raise HTTPException(400, str(e)) from e
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        clean = re.sub(r"[^A-Za-z0-9._ \-]", "_", Path(name).name)[:80] or "app.apk"
        row = db.add_attachment(bot_id, apkcheck.APK_MIME, str(path), 0, 0, size, aid, clean)
        return {"id": row["id"], "name": clean, "bytes": size, "kind": "apk"}

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
            meta["attachments"] = [{"id": a["id"], "kind": "apk", "name": a["name"], "bytes": a["bytes"]} if a["mime"] == apkcheck.APK_MIME else {"id": a["id"], "w": a["w"], "h": a["h"]} for a in pics]
        first_picture = any(a["mime"] != apkcheck.APK_MIME for a in pics) and not db.attachment_notice_sent(bot_id)
        if first_picture:  # said once per chat, before the picture goes anywhere
            orch.post(bot_id, "Pictures you attach are sent to the model provider, the same as your messages. Avoid photos of cards, IDs or other private documents.",
                      "bot_note", {"notice": "image-privacy"})
        only_apk = pics and all(a["mime"] == apkcheck.APK_MIME for a in pics)
        msg = db.add_message(bot_id, "user", "text", text or (f"APK: {pics[0]['name']}" if only_apk else images.IMAGE_ONLY), meta or None)
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
            elif body.use_relay:  # the code the owner's phone found, approved with one click: taken from memory here, typed on the phone, never shown
                got = jobs.otp.take(item_id) if jobs.otp else None
                if got is None:
                    raise HTTPException(409, "no code from your phone yet")
                label, resume = f"Code from {got[1]} sent to the phone", {"code": got[0]}
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
        if kind == "otp" and jobs.otp:
            jobs.otp.finish(item_id)  # stop watching the owner's phone and clear the card's masked hint
        if field and kind in ("text", "choice"):
            db.memory_add(item["bot_id"], f"Owner said {field}: {resume['value']}")
        if not item["job_id"] and kind == "secret" and resume["saved"]:
            threading.Thread(target=orch.turn, args=(item["bot_id"], f"The owner saved the secret '{resume['saved']}' in the vault. Continue with what they wanted."), daemon=True).start()
        if not item["job_id"] and user_text:
            msg = db.add_message(item["bot_id"], "user", "text", user_text)
            hub.publish(item["bot_id"], {"type": "message", "message": msg})
            if payload.get("unpair"):  # the owner chose what to do with a paired phone: carried out in code, never decided by a model turn
                threading.Thread(target=orch.unpair_decided, args=(item, user_text), daemon=True).start()
            elif payload.get("connector"):  # an app's tool the owner was asked to allow: run (or skip) in code, then the bot reports
                threading.Thread(target=orch.connector_decided, args=(item, bool(body.approved)), daemon=True).start()
            elif payload.get("web"):  # a "other ways" card: the pick is carried out in code (open a link, show it, or nothing), never decided by a model turn
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
        audit("job stopped by the owner", job=job_id)
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
        if item.get("kind") == "report":  # a generated page: no scripts, nothing from outside, and not allowed to do anything else even if opened on its own
            return FileResponse(item["path"], media_type="text/html; charset=utf-8", headers={
                "Cache-Control": "private, max-age=3600", "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff"})
        return FileResponse(item["path"], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/library/{lib_id}/md", dependencies=[Depends(auth)])
    def library_md(lib_id: str):
        item = db.get_library(lib_id)
        md = Path(item["path"]).with_suffix(".md") if item and item.get("kind") == "report" else None
        if not md or not md.exists():
            raise HTTPException(404, "no Markdown version")
        return FileResponse(md, media_type="text/markdown; charset=utf-8", headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    def phone_lane(phone: str | None) -> str:
        """`?phone=games` selects Cloud phone 2; no value is the shared emulator."""
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

    @app.post("/api/mobile/present", dependencies=[Depends(auth)])
    def mobile_present(phone: str | None = Query(default=None)):
        """The owner is looking at the page: keep the phone from pausing and, if it is paused or stopped, bring it back now so it is ready before the first request."""
        lane = phone_lane(phone)
        lifecycle = jobs.pair(lane)[1]
        if lifecycle:
            lifecycle.touch()
            if lifecycle.power in ("sleeping", "off"):
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

    @app.get("/api/knowledge", dependencies=[Depends(auth)])
    def knowledge_list(q: str = Query(default="", max_length=500), kind: str | None = Query(default=None, max_length=40), limit: int = Query(default=20, ge=1, le=20)):
        """The owner sees every stored note: the best matches for `q`, or the newest when there is no query."""
        viewer = ("owner", None)
        hits = db.knowledge.search(q, viewer=viewer, kinds=kind, limit=limit) if q.strip() else db.knowledge.recent(viewer, kinds=kind, limit=limit)
        return {"hits": hits, "stats": db.knowledge.stats()}

    @app.delete("/api/knowledge/{kid}", dependencies=[Depends(auth)])
    def knowledge_delete(kid: int):
        if not db.knowledge.delete(kid):
            raise HTTPException(404, "unknown note")
        return {"ok": True}

    @app.get("/api/vault", dependencies=[Depends(auth)])
    def vault_list():
        return vault.entries()

    @app.delete("/api/vault/{secret_id}", dependencies=[Depends(auth)])
    def vault_delete(secret_id: str):
        if not vault.delete(secret_id):
            raise HTTPException(404, "unknown secret")
        audit("vault secret deleted", id=secret_id)
        return {"ok": True}

    def connectors_changed():
        for b in db.list_bots():
            hub.publish(b["id"], {"type": "refresh", "what": "connectors"})

    @app.get("/api/connectors", dependencies=[Depends(auth)])
    def connectors_list():
        return orch.connectors.directory()

    @app.post("/api/connectors", dependencies=[Depends(auth)])
    def connectors_connect(body: Connect):
        try:
            row = orch.connectors.connect(body.id, body.custom, body.secret)
        except ConnectorError as e:
            raise HTTPException(422, str(e)) from None
        audit("connector connected", id=row["id"], transport=row["transport"])
        connectors_changed()
        return row

    @app.post("/api/connectors/{cid}/test", dependencies=[Depends(auth)])
    def connectors_test(cid: str):
        try:
            row = orch.connectors.test(cid)
        except ConnectorError as e:
            raise HTTPException(404, str(e)) from None
        connectors_changed()
        return row

    @app.delete("/api/connectors/{cid}", dependencies=[Depends(auth)])
    def connectors_delete(cid: str):
        if not orch.connectors.disconnect(cid):
            raise HTTPException(404, "unknown connector")
        audit("connector disconnected", id=cid)
        connectors_changed()
        return {"ok": True}

    @app.get("/api/logs", dependencies=[Depends(auth)])
    def logs_list():
        return {"dir": str(logs.log_dir() or ""), "files": logs.files()}

    @app.get("/api/logs/{name}", dependencies=[Depends(auth)])
    def logs_tail(name: str, lines: int = Query(default=200, ge=1, le=2000), level: str | None = Query(default=None, max_length=10), q: str | None = Query(default=None, max_length=100)):
        """The newest records of one log file (already redacted), optionally one level and up, or only those containing a word."""
        if name not in logs.FILES:
            raise HTTPException(404, "unknown log")
        return {"name": name, "lines": logs.tail(name, lines, level, q)}

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
