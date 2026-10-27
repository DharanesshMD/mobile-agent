"""Chat-first API: bots, messages, live events, action cards, library, and the shared computer."""
import asyncio
import json
import re
import threading

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field


class NewBot(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    persona: str = Field(default="", max_length=4000)


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Answer(BaseModel):
    approved: bool | None = None
    value: str | None = Field(default=None, max_length=500)
    choice: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=12)
    done: bool | None = None


class Control(BaseModel):
    action: str = Field(pattern="^(take|release)$")


def register_chat(app: FastAPI, auth, db, hub, jobs, orch) -> None:
    def bot_or_404(bot_id: str) -> dict:
        b = db.get_bot(bot_id)
        if not b:
            raise HTTPException(404, "unknown bot")
        return b

    @app.get("/api/bots", dependencies=[Depends(auth)])
    def list_bots():
        bots = db.list_bots()
        for b in bots:
            a = jobs.active_for(b["id"])
            b["active"] = a["status"] if a else None
        return bots

    @app.post("/api/bots", dependencies=[Depends(auth)])
    def create_bot(body: NewBot):
        return db.create_bot(body.name, body.persona)

    @app.get("/api/bots/{bot_id}/state", dependencies=[Depends(auth)])
    def bot_state(bot_id: str):
        b = bot_or_404(bot_id)
        return {"bot": b, "messages": db.messages(bot_id, 200), "last_seq": hub.last_seq(bot_id),
                "jobs": db.jobs(bot_id, 10), "computer": jobs.state()}

    @app.post("/api/bots/{bot_id}/messages", dependencies=[Depends(auth)])
    def post_message(bot_id: str, body: NewMessage):
        bot_or_404(bot_id)
        msg = db.add_message(bot_id, "user", "text", body.text.strip())
        hub.publish(bot_id, {"type": "message", "message": msg})
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
            label = user_text = text
            resume = {"value": text}
        elif kind == "handoff":
            label, resume, user_text = "Done", {"done": True}, "Done"
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
        db.close_item(item_id, "answered", None if kind == "otp" else label)
        if item["message_id"]:
            m = db.get_message(item["message_id"])
            upd = db.update_message(item["message_id"], meta={**m["meta"], "status": "answered", "answer": label})
            hub.publish(item["bot_id"], {"type": "message", "message": upd})
        if field and kind in ("text", "choice"):
            db.memory_add(item["bot_id"], f"Owner said {field}: {resume['value']}")
        if not item["job_id"] and user_text:
            msg = db.add_message(item["bot_id"], "user", "text", user_text)
            hub.publish(item["bot_id"], {"type": "message", "message": msg})
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

    @app.get("/api/computer/state", dependencies=[Depends(auth)])
    def computer_state():
        return jobs.state()

    @app.post("/api/computer/control", dependencies=[Depends(auth)])
    def computer_control(body: Control):
        jobs.take_control() if body.action == "take" else jobs.release_control()
        return jobs.state()
