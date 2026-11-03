"""Log files for the running app.

Everything lands in `data/logs/` (LOG_DIR), one folder, owner-only permissions, size-rotated (10 MB x 5 per file):
  app.log     every message of the app and its libraries (INFO and up; DEBUG with LOG_LEVEL=DEBUG)
  errors.log  WARNING and up, with tracebacks: the file to open when something broke
  access.log  one line per HTTP request on every listener (method, path, status, ms, caller address, request id)
  jobs.log    phone jobs and the orchestrator's turns: queued, started, step counts, finished, why
  a2a.log     the A2A door as JSON lines: who called what, which task, the outcome
  audit.log   what the owner changed (bots, connectors, A2A callers, the phone door) and who failed to sign in
A line reads `2026-11-03 14:02:11.123 INFO  agent.jobs [bot=ab12 job=cd34] started: open settings`.

What is NEVER written: message bodies, prompts, model replies, screenshots, file contents, pairing codes, tokens, keys, vault values. Every line also passes
through a redaction step (tokens, `Authorization` headers, `?token=`, card/ID numbers, passwords, e-mail addresses become `d***@host`) as the last thing before it
reaches a file, so a mistake at a call site cannot leak one. Crashes of any thread, unhandled exceptions and library warnings are captured too.
"""
import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from pathlib import Path

from agent import sensitive

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
FILES = ("app", "errors", "access", "jobs", "a2a", "audit")
LOGGER_FILES = {"app": None, "errors": None, "access": ("agent.access",), "jobs": ("agent.jobs", "agent.orch"), "a2a": ("agent.a2a",), "audit": ("agent.audit",)}

_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("log_ctx", default={})
_state = {"dir": None}
_lock = threading.Lock()

BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}")
KEYED = re.compile(r"(?i)\b(token|api[_-]?key|key|secret|password|passwd|authorization|code|signature)(=|:\s*)([^&\s\"',;]{3,})")
TOKENS = re.compile(r"\b(?:a2a_|dev_|sk-|ghp_|github_pat_|xox[bp]-|fc-|ctx7sk-|hf_|ntn_)[A-Za-z0-9_\-]{12,}")
LONG = re.compile(r"[A-Za-z0-9_\-]{40,}")
HEX = re.compile(r"[0-9a-fA-F]{40,}")  # a hash (sha256 of an APK) is fine to keep
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _mask_email(m: re.Match) -> str:
    name, _, domain = m.group(0).partition("@")
    return f"{name[:1]}***@{domain}"


def redact(text: str) -> str:
    """The last line of defence: nothing secret-looking leaves this function."""
    if not text:
        return text
    text = BEARER.sub(lambda m: m.group(1) + " ***", text)
    text = KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = TOKENS.sub("***", text)
    text = LONG.sub(lambda m: m.group(0) if HEX.fullmatch(m.group(0)) else "***", text)
    text = EMAIL.sub(_mask_email, text)
    try:
        text = sensitive.redact(text)[0]  # card numbers, ID numbers, "password is ..."
    except Exception:
        pass
    return text


@contextlib.contextmanager
def bind(**fields):
    """Fields (bot, job, task, client, req) that every log line written inside the block carries, in any thread that copies the context."""
    token = _ctx.set({**_ctx.get(), **{k: str(v) for k, v in fields.items() if v not in (None, "")}})
    try:
        yield
    finally:
        _ctx.reset(token)


def push(**fields):
    """bind() for code that cannot use a `with` block: keep the returned token and give it to pop() in a finally."""
    return _ctx.set({**_ctx.get(), **{k: str(v) for k, v in fields.items() if v not in (None, "")}})


def pop(token) -> None:
    _ctx.reset(token)


def context() -> dict:
    return dict(_ctx.get())


class _Context(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = dict(_ctx.get())
        return True


class _Text(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "ctx", None) or {}
        tag = " [" + " ".join(f"{k}={v}" for k, v in ctx.items()) + "]" if ctx else ""
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}"
        line = f"{t} {record.levelname:<5} {record.name}{tag} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return redact(line)


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name, **(getattr(record, "ctx", None) or {}),
                **(getattr(record, "fields", None) or {}), "msg": record.getMessage()}
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(data, ensure_ascii=False, default=str))


class _Only(logging.Filter):
    def __init__(self, names: tuple, invert: bool = False):
        super().__init__()
        self.names, self.invert = names, invert

    def filter(self, record: logging.LogRecord) -> bool:
        hit = any(record.name == n or record.name.startswith(n + ".") for n in self.names)
        return not hit if self.invert else hit


def _handler(path: Path, fmt: logging.Formatter, level: int) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    h.setFormatter(fmt)
    h.setLevel(level)
    h.addFilter(_Context())
    return h


def log_dir() -> Path | None:
    return _state["dir"]


def setup_logging(directory: str | Path | None = None, level: str | None = None, console: bool = True) -> Path:
    """Install the file handlers (once; calling again with the same folder does nothing) and the crash hooks. Returns the folder."""
    with _lock:
        d = Path(directory or os.environ.get("LOG_DIR", "data/logs"))
        if _state["dir"] == d:
            return d
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        teardown_logging()
        lvl = getattr(logging, (level or os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
        text, js = _Text(), _Json()
        root = logging.getLogger()
        root.setLevel(min(lvl, logging.INFO))
        logging.getLogger("agent").setLevel(lvl)
        handlers: list[logging.Handler] = []
        app = _handler(d / "app.log", text, lvl)
        app.addFilter(_Only(("agent.access",), invert=True))  # requests have their own file
        handlers.append(app)
        handlers.append(_handler(d / "errors.log", text, logging.WARNING))
        handlers.append(_handler(d / "access.log", text, lvl))
        handlers.append(_handler(d / "jobs.log", text, lvl))
        handlers.append(_handler(d / "a2a.log", js, lvl))
        handlers.append(_handler(d / "audit.log", text, lvl))
        for h, name in zip(handlers[2:], ("access", "jobs", "a2a", "audit")):
            h.addFilter(_Only(LOGGER_FILES[name]))
        if console:
            c = logging.StreamHandler(sys.stderr)
            c.setFormatter(text)
            c.setLevel(max(lvl, logging.INFO))
            c.addFilter(_Context())
            c.addFilter(_Only(("agent.access",), invert=True))
            handlers.append(c)
        for h in handlers:
            h._mobile_agent = True  # so teardown removes exactly these
            root.addHandler(h)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):  # the server's own messages go through the same files
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.propagate = True
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # requests are logged by the middleware, with timing and request ids
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.captureWarnings(True)
        _install_hooks()
        _state["dir"] = d
        for name in FILES:  # owner-only from the first byte
            f = d / f"{name}.log"
            if not f.exists():
                f.touch(mode=0o600)
            else:
                try:
                    os.chmod(f, 0o600)
                except OSError:
                    pass
        logging.getLogger("agent").info("logging to %s (level %s)", d, logging.getLevelName(lvl))
        return d


def teardown_logging() -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_mobile_agent", False)]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _state["dir"] = None


_hooks = {"installed": False}


def _install_hooks() -> None:
    """A crash in any thread, an exception nobody caught and an 'unraisable' error all end up in errors.log, not only on a terminal that may be gone."""
    if _hooks["installed"]:
        return
    _hooks["installed"] = True
    log = logging.getLogger("agent.crash")
    prev_exc, prev_thread, prev_unr = sys.excepthook, threading.excepthook, sys.unraisablehook

    def on_exc(t, v, tb):
        log.critical("uncaught exception", exc_info=(t, v, tb))
        prev_exc(t, v, tb)

    def on_thread(args):
        if args.exc_type is SystemExit:
            return
        log.error("thread %s crashed", getattr(args.thread, "name", "?"), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        prev_thread(args)

    def on_unraisable(u):
        log.error("unraisable error in %s: %s", getattr(u.object, "__qualname__", repr(u.object))[:80], u.err_msg or "", exc_info=(u.exc_type, u.exc_value, u.exc_traceback))
        prev_unr(u)

    sys.excepthook, threading.excepthook, sys.unraisablehook = on_exc, on_thread, on_unraisable


def audit(action: str, **fields) -> None:
    """Something the owner changed, or a refused sign-in: one line in audit.log. Values are short labels (a name, an id), never a secret."""
    parts = " ".join(f"{k}={str(v)[:80]}" for k, v in fields.items() if v not in (None, ""))
    logging.getLogger("agent.audit").info("%s %s", action, parts)


def event(logger: str, msg: str, level: int = logging.INFO, **fields) -> None:
    """A structured record (the fields become JSON keys in a2a.log)."""
    logging.getLogger(logger).log(level, msg, extra={"fields": {k: v for k, v in fields.items() if v is not None}})


# ---- HTTP requests
NOISY = re.compile(r"^(/api/bots(/[^/]+/(state|events|jobs|library|routines))?|/api/(mobile/state|phones|device/screenshot|health|gateway/state|connectors|a2a|knowledge|vault|me/history)|/fonts/|/vendor/|/connect\.js|/)$")
SECRET_QUERY = {"token", "key", "secret", "code", "password", "authorization", "signature"}


def clean_query(qs: str) -> str:
    out = []
    for pair in qs.split("&")[:12]:
        k, _, v = pair.partition("=")
        out.append(f"{k[:30]}=***" if k.lower() in SECRET_QUERY else f"{k[:30]}={v[:30]}")
    return "&".join(out)


class AccessLog:
    """A pure ASGI middleware (it does not buffer, so server-sent events and websockets are untouched): one line per request with a request id the
    other log lines of that request carry. `listener` names the door (main, gateway, a2a)."""

    def __init__(self, app, listener: str = "main"):
        self.app, self.listener = app, listener
        self.log = logging.getLogger("agent.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        rid = os.urandom(4).hex()
        t0 = time.time()
        status = {"code": 0, "bytes": 0}
        path = scope.get("path", "")
        method = scope.get("method", "WS")
        client = (scope.get("client") or ("-", 0))[0]
        qs = clean_query(scope.get("query_string", b"").decode("latin-1")) if scope.get("query_string") else ""

        async def wrapped(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid.encode())]}
            elif message["type"] == "http.response.body":
                status["bytes"] += len(message.get("body", b""))
            elif message["type"] == "websocket.accept":
                status["code"] = 101
            return await send(message)

        token = _ctx.set({**_ctx.get(), "req": rid})
        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            self.log.error("%s %s%s -> crashed %dms %s via=%s", method, path, "?" + qs if qs else "", (time.time() - t0) * 1000, client, self.listener, exc_info=True)
            raise
        finally:
            code = status["code"] or 0
            ms = (time.time() - t0) * 1000
            lvl = logging.ERROR if code >= 500 else logging.WARNING if code in (401, 403, 429) else logging.DEBUG if (code < 400 and method in ("GET", "WS") and NOISY.match(path)) else logging.INFO
            self.log.log(lvl, "%s %s%s %s %dms %dB %s via=%s", method, path, "?" + qs if qs else "", code or "-", ms, status["bytes"], client, self.listener)
            _ctx.reset(token)


# ---- reading them back (the owner's page and the tests)
def tail(name: str, lines: int = 200, level: str | None = None, contains: str | None = None, directory: Path | None = None) -> list[str]:
    """The last `lines` records of a log file (a record can span lines: a traceback belongs to the line above it), optionally only one level and up or containing text."""
    if name not in FILES:
        raise KeyError(name)
    d = directory or _state["dir"]
    if d is None or not (d / f"{name}.log").exists():
        return []
    lines = max(1, min(int(lines), 2000))
    size = (d / f"{name}.log").stat().st_size
    with open(d / f"{name}.log", "rb") as f:
        f.seek(max(0, size - 1_500_000))  # the last ~1.5 MB is plenty for a screenful
        raw = f.read().decode("utf-8", "replace")
    records, cur = [], []
    for ln in raw.splitlines():
        if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} |^\{\"ts\"", ln):
            if cur:
                records.append("\n".join(cur))
            cur = [ln]
        elif cur:
            cur.append(ln)
    if cur:
        records.append("\n".join(cur))
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
    floor = order.get((level or "").upper(), 0)
    needle = (contains or "").lower()

    def keep(r: str) -> bool:
        if needle and needle not in r.lower():
            return False
        if not floor:
            return True
        m = re.match(r"^\S+ \S+ (\w+)", r) or re.search(r'"level": "(\w+)"', r)
        return bool(m) and order.get(m.group(1).upper(), 0) >= floor

    return [redact(r) for r in records if keep(r)][-lines:]


def files() -> list[dict]:
    d = _state["dir"]
    if d is None:
        return []
    out = []
    for n in FILES:
        p = d / f"{n}.log"
        out.append({"name": n, "bytes": p.stat().st_size if p.exists() else 0, "modified": p.stat().st_mtime if p.exists() else None})
    return out
