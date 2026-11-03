# Connectors (MCP) for the phone agent: plan (3 Nov 2026)

Reference: the owner's screen recording of a connectors directory (search box "Search across apps and skills", category pills, two-column rows with "Connect" / "Connected", a custom/team section).
Goal: the agent can use an app's own API through MCP when one is connected, and keeps driving the phone when none is. Faster, cheaper, works while the phone sleeps, and nothing has to be tapped.

## What "connector" means here
A connector is an MCP server the owner connected: a hosted one over HTTPS (Streamable HTTP) or a local one the Mac starts (stdio). Its tools become actions the bots can call.
The phones stay the universal fallback: no connector for an app, or a connector that is down, and the bot does it on the phone as before.

## Decisions
- **Own small MCP client** (`agent/mcp.py`), no new dependency: JSON-RPC 2.0 over Streamable HTTP (POST, JSON or SSE reply, `Mcp-Session-Id`, `MCP-Protocol-Version`, re-initialise once on a 404 session) and over stdio (one JSON message per line). Only `initialize`, `tools/list`, `tools/call`.
- **Sign-in: token or key only in v1** (a Bearer token, an API key header, or an environment variable for a local server). OAuth browser flows are not built: servers that only offer OAuth are reachable only if they also accept a key. Said plainly in the UI.
- **Catalog** (`agent/connectors.py`): a short curated list of servers whose address and key header were checked against their own docs (GitHub, Context7, Hugging Face, Linear, plus two keyless local ones) and a **Custom MCP server** form for anything else.
  Nothing in the catalog is guessed; an entry that cannot be verified is not listed.
- **Secrets** go into the existing encrypted vault (label `connector:<id>`); never to a model, a log, a chat message or the page after saving.
- **Who can add one:** only the owner, from the page (or the API). A bot can never connect, change or remove a connector, and a model cannot make the Mac run a command.
- **Safety of use** (a connector is untrusted code and untrusted text):
  - Tool results are data, never instructions (same rule as web pages and pictures): wrapped, size-capped (8 KB), and the prompt says so.
  - A tool that only reads (MCP annotation `readOnlyHint: true`) runs at once. **Every other tool, and any tool without that hint, needs the owner's approval for that very call**: a card shows the connector, the tool and the exact arguments; Approve runs it in code (not decided by a model turn), Decline does nothing.
  - Owner-supplied arguments with secrets (card numbers, passwords) are hidden by the same redaction as chat.
  - At most 3 connector calls per turn, 20 s per call, a connector that fails 3 times in a row is marked "Not responding" until the owner reconnects.
  - Connections are per owner, not per bot, so every bot can use them; a bot's context lists only the connected ones.
- **Routing rule** (prompt): if a connected connector can do the job, use it before the phone; use the phone when the task needs the app's screen, a sign-in the connector does not cover, or there is no connector.

## Pieces
| Piece | What |
|---|---|
| `agent/mcp.py` | the client (HTTP and stdio), `McpError`, `list_tools`, `call_tool` |
| `agent/connectors.py` | catalog, `Connectors` store (table `connectors`), connect/test/disconnect, tool cache, approval gate, context text for the model |
| `db.py` | table `connectors(id, kind, name, transport, url, command, env_keys, secret_label, status, tools, error, created, updated)` |
| `chat_api.py` | `GET /api/connectors` (catalog + state), `POST /api/connectors` (connect: catalog id or custom), `POST /api/connectors/{id}/test`, `DELETE /api/connectors/{id}` |
| `orchestrator.py` | action `connector_call {connector, tool, args}`, context block, approval card, `connector_approved` |
| `prompts/orchestrator.md` | the action, the routing rule, the data rule |
| `web/index.html` | the directory (the recording's layout), connect dialog, Connected state, custom form |
| `tests/test_connectors.py`, `tests/test_mcp.py` | fake HTTP and stdio MCP servers; approval, injection, caps, secrets |

## Not in v1 (said honestly)
OAuth sign-in (Gmail, Calendar, Notion, Slack hosted servers), MCP resources and prompts, server-initiated requests (sampling), per-bot connector scoping, and exposing the phones as an MCP server for other apps (next step).

## Status (3 Nov 2026)
Built and tested: client (HTTP and stdio), catalog of seven entries, custom server form, vault-hidden keys, `connector_call` with the approval gate, API, directory page.
Tests: `tests/test_mcp.py` (client against a fake server over both transports), `tests/test_connectors.py` (store, secrets, approval, injection, limits, API).
Live: the keyless Fetch server connected for real (first start 18 s, it downloads a small package) and a chat message "read https://example.com and tell me its heading" was answered through it in 16 s with no phone job.
Not run live (need your own keys): GitHub, Linear, Context7, Hugging Face, Notion, Firecrawl. Their addresses and key headers come from each server's own docs; a wrong key shows a readable error and stores nothing.
