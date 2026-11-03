#!/usr/bin/env python3
"""A small A2A client for this agent (standard library only): read the card, send a task, stream it, poll it, cancel it, search the shared knowledge.

  export A2A_URL=http://127.0.0.1:8766 A2A_TOKEN=a2a_...        (the token comes from the Agents page, shown once)
  python scripts/a2a_client.py card
  python scripts/a2a_client.py send "Open Settings and tell me the Android version" --skill phone-task
  python scripts/a2a_client.py send "What is in this?" --file report.pdf --file photo.png
  python scripts/a2a_client.py send "hello" --stream
  python scripts/a2a_client.py get <task id>        cancel <task id>        list
  python scripts/a2a_client.py retrieve "flight booking"      store "the customer prefers evening delivery"
  python scripts/a2a_client.py download <file url> out.png
Add --v03 to speak the 0.3 dialect (message/send, tasks/get, ...) and --json to print raw responses.
"""
import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DONE = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "completed", "failed", "canceled", "rejected"}
PAUSED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED", "input-required", "auth-required"}


class Client:
    def __init__(self, url: str, token: str, v03: bool = False):
        self.url, self.token, self.v03 = url.rstrip("/"), token, v03
        self.n = 0

    def _req(self, path: str, body=None, method=None, accept="application/json"):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Accept": accept, "A2A-Version": "0.3" if self.v03 else "1.0"}
        if data is not None:
            h["Content-Type"] = "application/json"
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, method=method or ("POST" if data is not None else "GET"), headers=h), timeout=120)

    def card(self) -> dict:
        with self._req("/.well-known/agent-card.json" + ("?version=0.3" if self.v03 else "")) as r:
            return json.load(r)

    def methods(self) -> dict:
        return ({"send": "message/send", "stream": "message/stream", "get": "tasks/get", "cancel": "tasks/cancel"} if self.v03
                else {"send": "SendMessage", "stream": "SendStreamingMessage", "get": "GetTask", "cancel": "CancelTask", "list": "ListTasks"})

    def rpc(self, op: str, params: dict) -> dict:
        self.n += 1
        try:
            with self._req("/a2a", {"jsonrpc": "2.0", "id": self.n, "method": self.methods()[op], "params": params}) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:300]}")
        if "error" in out:
            raise SystemExit(f"error {out['error']['code']}: {out['error']['message']}")
        return out["result"]

    def message(self, parts: list, **extra) -> dict:
        return {"messageId": str(uuid.uuid4()), "role": "user" if self.v03 else "ROLE_USER", "parts": [self.part(p) for p in parts], **extra}

    def part(self, p: dict) -> dict:
        if not self.v03:
            return p
        if "text" in p:
            return {"kind": "text", "text": p["text"]}
        if "data" in p:
            return {"kind": "data", "data": p["data"]}
        return {"kind": "file", "file": {"bytes": p["raw"], "name": p.get("filename"), "mimeType": p.get("mediaType")}}

    def send(self, parts: list, wait: float = 120, **extra) -> dict:
        cfg = {"blocking": False} if self.v03 else {"returnImmediately": True}
        res = self.rpc("send", {"message": self.message(parts, **extra), "configuration": cfg})
        task = res.get("task", res)
        end = time.time() + wait
        while task["status"]["state"] not in DONE | PAUSED and time.time() < end:
            time.sleep(1)
            task = self.rpc("get", {"id": task["id"]})
        return task

    def stream(self, parts: list, **extra):
        self.n += 1
        body = {"jsonrpc": "2.0", "id": self.n, "method": self.methods()["stream"], "params": {"message": self.message(parts, **extra)}}
        with self._req("/a2a", body, accept="text/event-stream") as r:
            data = []
            for raw in r:
                line = raw.decode().rstrip("\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    yield json.loads("\n".join(data))["result"]
                    data = []


def file_part(path: str) -> dict:
    p = Path(path)
    mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return {"raw": base64.b64encode(p.read_bytes()).decode(), "filename": p.name, "mediaType": mt}


def show_task(t: dict, out) -> None:
    st = t["status"]
    msg = st.get("message") or {}
    text = " ".join(p.get("text", "") for p in msg.get("parts", []))
    print(f"task {t['id']}  context {t['contextId']}  state {st['state']}", file=out)
    if text:
        print(f"  {text}", file=out)
    for a in t.get("artifacts", []):
        for p in a["parts"]:
            if "text" in p:
                print(f"  [{a['artifactId']}] {p['text']}", file=out)
            elif "data" in p:
                print(f"  [{a['artifactId']}] data: {json.dumps(p['data'], ensure_ascii=False)[:600]}", file=out)
            else:
                f = p.get("file", p)
                print(f"  [{a['artifactId']}] file {f.get('filename') or f.get('name')} ({f.get('mediaType') or f.get('mimeType')}): {f.get('url') or f.get('uri')}", file=out)


def main(argv=None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("A2A_URL", "http://127.0.0.1:8766"))
    ap.add_argument("--token", default=os.environ.get("A2A_TOKEN", ""))
    ap.add_argument("--v03", action="store_true", help="speak the 0.3 dialect")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("card")
    s = sub.add_parser("send")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--file", action="append", default=[])
    s.add_argument("--skill", default="")
    s.add_argument("--data", default="", help="a JSON object for a data part")
    s.add_argument("--context", default="")
    s.add_argument("--task", default="")
    s.add_argument("--stream", action="store_true")
    s.add_argument("--wait", type=float, default=120)
    for name in ("get", "cancel"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("list")
    r = sub.add_parser("retrieve")
    r.add_argument("query")
    r.add_argument("--limit", type=int, default=5)
    st = sub.add_parser("store")
    st.add_argument("text")
    st.add_argument("--title", default="")
    d = sub.add_parser("download")
    d.add_argument("file_url")
    d.add_argument("dest")
    a = ap.parse_args(argv)
    c = Client(a.url, a.token, a.v03)

    def emit(obj):
        print(json.dumps(obj, indent=2, ensure_ascii=False), file=out)

    if a.cmd == "card":
        emit(c.card())
    elif a.cmd == "send" or a.cmd == "retrieve" or a.cmd == "store":
        parts, extra = [], {}
        if a.cmd == "send":
            if a.text:
                parts.append({"text": a.text})
            payload = json.loads(a.data) if a.data else {}
            if a.skill:
                payload["skill"] = a.skill
            if payload:
                parts.append({"data": payload})
            parts += [file_part(f) for f in a.file]
            if a.context:
                extra["contextId"] = a.context
            if a.task:
                extra["taskId"] = a.task
        elif a.cmd == "retrieve":
            parts = [{"data": {"skill": "retrieve-context", "query": a.query, "limit": a.limit}}]
        else:
            parts = [{"data": {"skill": "store-context", "text": a.text, **({"title": a.title} if a.title else {})}}]
        if a.cmd == "send" and a.stream:
            for ev in c.stream(parts, **extra):
                (emit if a.json else lambda e: print(json.dumps(e, ensure_ascii=False)[:400], file=out))(ev)
        else:
            task = c.send(parts, getattr(a, "wait", 120), **extra)
            emit(task) if a.json else show_task(task, out)
    elif a.cmd == "get":
        t = c.rpc("get", {"id": a.id})
        emit(t) if a.json else show_task(t, out)
    elif a.cmd == "cancel":
        t = c.rpc("cancel", {"id": a.id})
        emit(t) if a.json else show_task(t, out)
    elif a.cmd == "list":
        if a.v03:
            raise SystemExit("the 0.3 dialect has no list method")
        res = c.rpc("list", {})
        for t in res["tasks"]:
            print(f"{t['id']}  {t['status']['state']}  context {t['contextId']}", file=out)
    elif a.cmd == "download":
        req = urllib.request.Request(a.file_url, headers={"Authorization": f"Bearer {a.token}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            Path(a.dest).write_bytes(resp.read())
        print(f"saved {a.dest}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
