"""Connectors: MCP servers the owner connected, so a bot can use an app's own tools instead of tapping through its screen.

The owner adds them (from the page); a bot can only call a tool that a connected server listed. Keys live in the encrypted vault under hidden labels.
Everything a server returns is untrusted data. See docs/connectors.md for the rules.
"""
import atexit
import json
import os
import re
import threading
import time

from agent import mcp
from agent.db import Database
from agent.vault import HIDDEN, Vault

CALL_RESULT_CAP = 8000
IDLE_S = 120  # a local server that nobody used for this long is stopped
FAIL_LIMIT = 3
CATEGORIES = ["Productivity", "Code", "Data", "Research"]

# Addresses and key headers below were checked against each server's own documentation (3 Nov 2026). `auth.kind`: bearer = "Authorization: Bearer <key>",
# header = a named header, env = an environment variable for a local server, none = no key.
CATALOG = [
    {"id": "github", "name": "GitHub", "category": "Code", "blurb": "Repositories, issues and pull requests", "transport": "http", "url": "https://api.githubcopilot.com/mcp/",
     "auth": {"kind": "bearer", "label": "GitHub personal access token", "help_url": "https://github.com/settings/personal-access-tokens", "help": "Create a fine-grained token with only the repositories and permissions you want the agent to use."}},
    {"id": "linear", "name": "Linear", "category": "Productivity", "blurb": "Issues, projects and cycles", "transport": "http", "url": "https://mcp.linear.app/mcp",
     "auth": {"kind": "bearer", "label": "Linear API key", "help_url": "https://linear.app/settings/account/security", "help": "A personal API key (Settings, Security and access). It acts as you."}},
    {"id": "context7", "name": "Context7", "category": "Code", "blurb": "Up-to-date documentation for any library", "transport": "http", "url": "https://mcp.context7.com/mcp",
     "auth": {"kind": "header", "header": "CONTEXT7_API_KEY", "label": "Context7 API key (optional)", "optional": True, "help_url": "https://context7.com/dashboard", "help": "Works without a key at a lower rate limit."}},
    {"id": "huggingface", "name": "Hugging Face", "category": "Data", "blurb": "Models, datasets, Spaces and papers", "transport": "http", "url": "https://huggingface.co/mcp",
     "auth": {"kind": "bearer", "label": "Hugging Face token, read access (optional)", "optional": True, "help_url": "https://huggingface.co/settings/tokens", "help": "Works without a token with the default tools."}},
    {"id": "fetch", "name": "Fetch", "category": "Research", "blurb": "Read any web page as clean text", "transport": "stdio", "command": "uvx mcp-server-fetch",
     "auth": {"kind": "none"}, "local": True},
    {"id": "notion", "name": "Notion", "category": "Productivity", "blurb": "Pages and databases you share with the integration", "transport": "stdio", "command": "npx -y @notionhq/notion-mcp-server",
     "auth": {"kind": "env", "env": "NOTION_TOKEN", "label": "Notion integration token", "help_url": "https://www.notion.so/profile/integrations", "help": "Create an internal integration, then share only the pages it may use with it."}, "local": True},
    {"id": "firecrawl", "name": "Firecrawl", "category": "Research", "blurb": "Scrape and search the web", "transport": "stdio", "command": "npx -y firecrawl-mcp",
     "auth": {"kind": "env", "env": "FIRECRAWL_API_KEY", "label": "Firecrawl API key", "help_url": "https://www.firecrawl.dev/app/api-keys", "help": "Sent to Firecrawl with each request."}, "local": True},
]
BY_ID = {c["id"]: c for c in CATALOG}
CUSTOM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")


class ConnectorError(Exception):
    """A refusal the owner can read: bad input, unknown connector, the server said no."""


def commands_allowed() -> bool:
    """A custom command runs on this Mac. Off unless the owner turned it on in the environment; the catalog's fixed commands need no switch."""
    return os.environ.get("CONNECTORS_ALLOW_COMMANDS", "").strip().lower() in ("1", "true", "yes")


def signature(tool: dict) -> str:
    """name(arg*, arg) from the tool's JSON schema (* = required): enough for a model to fill in the arguments."""
    props = (tool.get("schema") or {}).get("properties") or {}
    req = set((tool.get("schema") or {}).get("required") or [])
    args = ", ".join(f"{k}{'*' if k in req else ''}" for k in list(props)[:12])
    return f"{tool['name']}({args})"


class Connectors:
    def __init__(self, db: Database, vault: Vault, opener=mcp.open_client):
        self.db, self.vault, self.opener = db, vault, opener
        self._live: dict[str, tuple] = {}  # id -> (client, last used)
        self._locks: dict[str, threading.Lock] = {}  # one call at a time per connector
        self._guard = threading.Lock()
        atexit.register(self.close_all)  # local servers must not outlive the app

    # ---- what the page and the models see
    def rows(self) -> list[dict]:
        return self.db._q("select * from connectors order by name")

    def get(self, cid: str) -> dict | None:
        return self.db._one("select * from connectors where id=?", (cid,))

    def public(self, row: dict) -> dict:
        return {"id": row["id"], "kind": row["kind"], "name": row["name"], "transport": row["transport"], "status": row["status"], "error": row["error"],
                "tools": [{"name": t["name"], "description": t["description"], "read_only": t["read_only"]} for t in row["tools"]], "created": row["created"]}

    def directory(self) -> dict:
        mine = {r["id"]: r for r in self.rows()}
        entries = []
        for c in CATALOG:
            auth = c["auth"]
            entries.append({"id": c["id"], "name": c["name"], "category": c["category"], "blurb": c["blurb"], "transport": c["transport"], "local": bool(c.get("local")),
                            "auth": {k: auth.get(k) for k in ("kind", "label", "optional", "help", "help_url")},
                            "connection": self.public(mine[c["id"]]) if c["id"] in mine else None})
        custom = [self.public(r) for r in mine.values() if r["kind"] == "custom"]
        return {"categories": CATEGORIES, "catalog": entries, "custom": custom, "commands_allowed": commands_allowed()}

    def context(self) -> str:
        """The block that tells every bot which tools it can call (empty when nothing is connected)."""
        lines = []
        for r in self.rows():
            if r["status"] != "connected":
                continue
            tools = "; ".join(f"{signature(t)}: {t['description'][:90]}{'' if t['read_only'] else ' [asks the owner first]'}" for t in r["tools"][:30])
            lines.append(f"- {r['id']} ({r['name']}): {tools or 'no tools'}")
        return "\n".join(lines)[:6000]

    # ---- connect / test / disconnect
    def _spec(self, row: dict, secret: str | None) -> dict:
        auth = row["auth"] or {}
        if row["transport"] == "stdio":
            return {"transport": "stdio", "command": row["command"], "env": {auth["env"]: secret} if auth.get("kind") == "env" and secret else {}}
        headers = {}
        if secret and auth.get("kind") == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif secret and auth.get("kind") == "header" and auth.get("header"):
            headers[auth["header"]] = secret
        return {"transport": "http", "url": row["url"], "headers": headers}

    def _secret(self, row: dict) -> str | None:
        label = row.get("secret_label")
        if not label:
            return None
        try:
            return self.vault.reveal(label, None, "connector", internal=True)
        except KeyError:
            return None

    def connect(self, catalog_id: str | None = None, custom: dict | None = None, secret: str = "") -> dict:
        secret = (secret or "").strip()
        if catalog_id:
            c = BY_ID.get(catalog_id)
            if not c:
                raise ConnectorError("unknown connector")
            cid, kind, name, transport = c["id"], "catalog", c["name"], c["transport"]
            url, command, auth = c.get("url", ""), c.get("command", ""), dict(c["auth"])
        elif custom:
            name = str(custom.get("name", "")).strip()[:40]
            cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30]
            if not CUSTOM_ID.match(cid or ""):
                raise ConnectorError("give it a name of at least two letters or digits")
            if cid in BY_ID:
                raise ConnectorError(f"'{name}' is a built-in entry: connect it from the list")
            kind, transport, url, command = "custom", "http", str(custom.get("url", "")).strip(), ""
            if str(custom.get("command", "")).strip():
                if not commands_allowed():
                    raise ConnectorError("running a command on this Mac is switched off (set CONNECTORS_ALLOW_COMMANDS=1 to allow it); use a server address instead")
                transport, command, url = "stdio", str(custom["command"]).strip(), ""
            a = custom.get("auth") if isinstance(custom.get("auth"), dict) else {}
            akind = a.get("kind") if a.get("kind") in ("none", "bearer", "header", "env") else "none"
            auth = {"kind": akind}
            if akind == "header":
                h = str(a.get("header", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9-]{1,60}", h):
                    raise ConnectorError("the header name must be letters, digits and dashes")
                auth["header"] = h
            if akind == "env":
                e = str(a.get("env", "")).strip()
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,59}", e):
                    raise ConnectorError("the variable name must be CAPITALS, digits and underscores")
                auth["env"] = e
        else:
            raise ConnectorError("choose a connector or describe a custom one")
        if transport == "http":
            try:
                url = mcp.check_url(url)
            except mcp.McpError as e:
                raise ConnectorError(str(e)) from None
        if auth["kind"] != "none" and not secret and not auth.get("optional"):
            raise ConnectorError(f"{auth.get('label') or 'a key'} is required")
        if auth["kind"] == "none":
            secret = ""
        label = f"{HIDDEN}{cid}" if secret and auth["kind"] != "none" else None
        row = {"id": cid, "kind": kind, "name": name, "transport": transport, "url": url, "command": command, "auth": auth, "secret_label": label}
        try:
            tools = self._probe(row, secret or None)
        except mcp.McpError as e:
            raise ConnectorError(f"{name} did not connect: {e}") from None
        self.disconnect(cid)  # replacing a connection: only now, after the new one proved to work
        if label:
            self.vault.put(label, secret, "note", internal=True)
        now = time.time()
        self.db._x("insert into connectors(id,kind,name,transport,url,command,auth,secret_label,status,tools,error,fails,created,updated) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, kind, name, transport, url, command, json.dumps(auth), label, "connected", json.dumps(tools), "", 0, now, now))
        return self.public(self.get(cid))

    def _probe(self, row: dict, secret: str | None) -> list[dict]:
        client = self.opener(self._spec(row, secret))
        try:
            return mcp.list_tools(client)
        finally:
            client.close()

    def test(self, cid: str) -> dict:
        row = self.get(cid)
        if not row:
            raise ConnectorError("unknown connector")
        self._drop_live(cid)
        try:
            tools = self._probe(row, self._secret(row))
        except mcp.McpError as e:
            self.db._x("update connectors set status='error', error=?, updated=? where id=?", (str(e)[:200], time.time(), cid))
            return self.public(self.get(cid))
        self.db._x("update connectors set status='connected', error='', fails=0, tools=?, updated=? where id=?", (json.dumps(tools), time.time(), cid))
        return self.public(self.get(cid))

    def disconnect(self, cid: str) -> bool:
        row = self.get(cid)
        if not row:
            return False
        self._drop_live(cid)
        if row["secret_label"]:
            self.vault.drop_internal(row["secret_label"])
        self.db._x("delete from connectors where id=?", (cid,))
        return True

    def erase_all(self) -> None:
        for r in self.rows():
            self.disconnect(r["id"])

    # ---- calling a tool
    def tool(self, cid: str, name: str) -> dict | None:
        row = self.get(cid)
        if not row or row["status"] != "connected":
            return None
        return next((t for t in row["tools"] if t["name"] == name), None)

    def _drop_live(self, cid: str) -> None:
        with self._guard:
            live = self._live.pop(cid, None)
        if live:
            try:
                live[0].close()
            except Exception:
                pass

    def _take(self, cid: str):
        """The live client of a connector (None if there is none, it died, or it sat idle), and the lock for using it. Idle ones are stopped."""
        now = time.time()
        with self._guard:
            for k in [k for k, v in self._live.items() if now - v[1] > IDLE_S]:
                try:
                    self._live.pop(k)[0].close()
                except Exception:
                    pass
            live = self._live.get(cid)
            if live and not getattr(live[0], "alive", lambda: True)():
                self._live.pop(cid, None)
                live = None
            return (live[0] if live else None), self._locks.setdefault(cid, threading.Lock())

    def call(self, cid: str, name: str, args: dict) -> str:
        """Run one tool and return its result as plain text for the model. Raises ConnectorError for anything the model should hear as an error."""
        row = self.get(cid)
        if not row:
            raise ConnectorError(f"no connector '{cid}' is connected")
        if row["status"] != "connected":
            raise ConnectorError(f"{row['name']} is not responding ({row['error'] or 'reconnect it from the Connectors page'})")
        if not self.tool(cid, name):
            raise ConnectorError(f"{row['name']} has no tool '{name}'")
        if not isinstance(args, dict):
            raise ConnectorError("arguments must be an object")
        client, lock = self._take(cid)
        with lock:
            try:
                client = client or self.opener(self._spec(row, self._secret(row)))
                result = mcp.call_tool(client, name, args)
                with self._guard:
                    self._live[cid] = (client, time.time())
            except mcp.McpError as e:
                self._drop_live(cid)
                fails = row["fails"] + 1
                down = fails >= FAIL_LIMIT
                self.db._x("update connectors set fails=?, status=?, error=?, updated=? where id=?", (fails, "error" if down else "connected", str(e)[:200] if down else "", time.time(), cid))
                raise ConnectorError(f"{row['name']} failed: {e}") from None
        if row["fails"]:
            self.db._x("update connectors set fails=0 where id=?", (cid,))
        return mcp.render(result, CALL_RESULT_CAP)

    def close_all(self) -> None:
        with self._guard:
            live, self._live = list(self._live.values()), {}
        for c in live:
            try:
                c[0].close()
            except Exception:
                pass
