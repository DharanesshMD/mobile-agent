"""A small MCP client: just enough of the Model Context Protocol to list a server's tools and call them.

Two transports, both JSON-RPC 2.0: Streamable HTTP (a hosted server; POST, the reply is JSON or an SSE stream) and stdio (a local server the Mac starts; one JSON message per line).
Only initialize, tools/list and tools/call are used. Whatever a server sends back is untrusted data: callers cap it and never treat it as instructions.
"""
import json
import os
import shlex
import subprocess
import threading
import time
from urllib.parse import urlparse

import httpx

PROTOCOL = "2025-06-18"
CLIENT_INFO = {"name": "mobile-agent", "version": "0.1.0"}
TIMEOUT_S = 20
MAX_PAGES = 5  # tools/list pages followed


class McpError(Exception):
    """The server could not be reached, refused, answered badly or timed out."""


def check_url(url: str) -> str:
    """https only; plain http only for this machine (a local server). Anything else could send a key over an open network."""
    u = urlparse(url.strip())
    if u.scheme == "https" and u.hostname:
        return url.strip()
    if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"):
        return url.strip()
    raise McpError("the address must start with https:// (http:// is allowed only for localhost)")


def _result(msg: dict):
    if "error" in msg:
        e = msg["error"] or {}
        raise McpError(f"{e.get('message', 'the server returned an error')} (code {e.get('code', '?')})")
    return msg.get("result")


class HttpClient:
    def __init__(self, url: str, headers: dict | None = None, timeout: float = TIMEOUT_S):
        self.url, self.timeout, self.session, self.n = check_url(url), timeout, None, 0
        self.headers = {k: v for k, v in (headers or {}).items() if k and v}
        self.http = httpx.Client(timeout=timeout, follow_redirects=False)  # a redirect could carry the key to another host

    def _hdrs(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        return h

    def _send(self, msg: dict, want_id: int | None):
        try:
            with self.http.stream("POST", self.url, json=msg, headers=self._hdrs()) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    raise McpError("the server redirected the request; check the address")
                if r.status_code in (401, 403):
                    raise McpError("the server refused the key or token (it may be wrong, expired or missing a permission)")
                if r.status_code == 404 and self.session:
                    raise _SessionGone()
                if r.status_code >= 400:
                    raise McpError(f"the server answered HTTP {r.status_code}")
                self.session = r.headers.get("mcp-session-id") or self.session
                if want_id is None:
                    return None
                if "text/event-stream" in r.headers.get("content-type", ""):
                    data = []
                    for line in r.iter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            msg_in = _loads("\n".join(data))
                            data = []
                            if isinstance(msg_in, dict) and msg_in.get("id") == want_id:
                                return msg_in
                    if data:
                        msg_in = _loads("\n".join(data))
                        if isinstance(msg_in, dict) and msg_in.get("id") == want_id:
                            return msg_in
                    raise McpError("the server's stream ended without an answer")
                body = _loads(r.read().decode("utf-8", "replace"))
                items = body if isinstance(body, list) else [body]
                for it in items:
                    if isinstance(it, dict) and it.get("id") == want_id:
                        return it
                raise McpError("the server's answer did not match the request")
        except httpx.TimeoutException:
            raise McpError(f"the server did not answer within {round(self.timeout)} s") from None
        except httpx.HTTPError as e:
            raise McpError(f"could not reach the server ({type(e).__name__})") from None

    def start(self) -> None:
        self.session = None
        r = self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO}, reinit=False)
        if not isinstance(r, dict):
            raise McpError("the server did not answer the hello")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)

    def _request(self, method: str, params: dict | None, reinit: bool = True):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        try:
            return _result(self._send(msg, self.n))
        except _SessionGone:
            if not reinit:
                raise McpError("the server lost the session") from None
            self.start()  # the server dropped the session (a restart or a timeout): one fresh start
            return self._request(method, params, reinit=False)

    def request(self, method: str, params: dict | None = None):
        return self._request(method, params)

    def close(self) -> None:
        try:
            if self.session:
                self.http.delete(self.url, headers=self._hdrs())
        except Exception:
            pass
        self.http.close()


class _SessionGone(Exception):
    pass


def _loads(text: str):
    try:
        return json.loads(text)
    except ValueError:
        raise McpError("the server sent something that is not JSON") from None


class StdioClient:
    """A local server started by the Mac. It gets a clean environment (PATH and HOME plus the variables the owner set), never the agent's own secrets."""

    def __init__(self, command: str, env: dict | None = None, timeout: float = TIMEOUT_S):
        self.timeout, self.n, self.replies = timeout, 0, {}
        self.cv = threading.Condition()
        argv = shlex.split(command)
        if not argv:
            raise McpError("a command is required")
        clean = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "SHELL") if k in os.environ}
        clean.update({k: v for k, v in (env or {}).items() if k and v})
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=clean, text=True, bufsize=1)
        except (OSError, ValueError) as e:
            raise McpError(f"could not start the server ({type(e).__name__}: {str(e)[:80]})") from None
        self.closed = False
        threading.Thread(target=self._read, daemon=True, name="mcp-stdio").start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a log line on stdout
            if isinstance(msg, dict) and "method" in msg and "id" in msg:  # the server asks us something (sampling, roots): not supported
                self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
            elif isinstance(msg, dict) and "id" in msg:
                with self.cv:
                    self.replies[msg["id"]] = msg
                    self.cv.notify_all()
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def _write(self, msg: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError("the server closed the connection") from None

    def start(self) -> None:
        r = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO})
        if not isinstance(r, dict):
            raise McpError("the server did not answer the hello")
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, method: str, params: dict | None = None):
        self.n += 1
        n = self.n
        self._write({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}})
        end = time.time() + self.timeout
        with self.cv:
            while n not in self.replies:
                left = end - time.time()
                if self.closed:
                    raise McpError("the server stopped")
                if left <= 0:
                    raise McpError(f"the server did not answer within {round(self.timeout)} s")
                self.cv.wait(left)
            return _result(self.replies.pop(n))

    def alive(self) -> bool:
        return self.proc.poll() is None and not self.closed

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        for f in (self.proc.stdout, self.proc.stdin):
            try:
                f.close()
            except Exception:
                pass


def open_client(spec: dict):
    """spec: {"transport": "http"|"stdio", "url", "headers"} or {"command", "env"}. Returns a started client."""
    if spec.get("transport") == "stdio":
        c = StdioClient(spec.get("command", ""), spec.get("env"))
    else:
        c = HttpClient(spec.get("url", ""), spec.get("headers"))
    try:
        c.start()
    except Exception:
        c.close()
        raise
    return c


def list_tools(client) -> list[dict]:
    """[{name, description, read_only, schema}] following next-page cursors (a few pages at most)."""
    out, cursor = [], None
    for _ in range(MAX_PAGES):
        r = client.request("tools/list", {"cursor": cursor} if cursor else {})
        if not isinstance(r, dict):
            raise McpError("the server's tool list was not understood")
        for t in r.get("tools") or []:
            if isinstance(t, dict) and isinstance(t.get("name"), str):
                ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
                out.append({"name": t["name"][:80], "description": str(t.get("description") or t.get("title") or "")[:300],
                            "read_only": ann.get("readOnlyHint") is True and ann.get("destructiveHint") is not True,  # only a server that says so, in so many words, is trusted to read only
                            "schema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}})
        cursor = r.get("nextCursor")
        if not cursor:
            break
    return out


def call_tool(client, name: str, args: dict) -> dict:
    r = client.request("tools/call", {"name": name, "arguments": args})
    if not isinstance(r, dict):
        raise McpError("the server's answer was not understood")
    return r


def render(result: dict, cap: int = 8000) -> str:
    """A tool result as plain text for the model: text parts, a note for anything else, cut at `cap`."""
    parts = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text", "")))
        elif isinstance(c, dict):
            parts.append(f"[{c.get('type', 'content')} omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts).strip() or "(empty result)"
    if len(text) > cap:
        text = text[:cap] + f"\n[cut: {len(text) - cap} more characters]"
    return ("ERROR FROM THE TOOL: " if result.get("isError") else "") + text
