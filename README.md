# Mobile Agent

An always-on, proactive agent that lives on a self-hosted Android environment in the cloud.
Control it from a web chatbot or a mobile client app, including always-on voice triggers.
The LLM drives the Android device to complete tasks and fetch information.

Status: planning. See [docs/plan.md](docs/plan.md).

Built for the Meta Global AI Developer Hackathon (Oct 27 - Nov 4, 2026). All code in this repo is new work started during the event.

## Run the agent (local emulator)

See [docs/local-device.md](docs/local-device.md) to boot the emulator, copy `.env.example` to `.env` and set `META_API_KEY`, then:

```bash
uv run python -m agent "Open Settings, go to Battery and tell me the battery percentage" --model muse-wonder-1.0
uv run python -m unittest discover -s tests
```

Each run writes screenshots and model replies to `runs/<timestamp>/` (gitignored).

## Run the service and chat UI

```bash
uv run python -m agent.service        # http://127.0.0.1:8000, uses the emulator and .env
```

Set `APP_TOKEN` to require a bearer token (mandatory if `HOST` is not loopback). The web UI shows a live phone view, streams each
agent step, and shows cards when the agent needs you: approval (`ask`), a handoff to sign in yourself (`handoff`), or a one-time
SMS code (`request_otp`). When the agent is idle or paused you can click the phone view to tap, and type or scroll yourself.

API: `POST /api/runs`, `GET /api/runs/{id}/events` (SSE), `POST /api/runs/{id}/resume`, `POST /api/runs/{id}/cancel`,
`GET /api/device/screenshot`, `POST /api/device/{tap,type,key,swipe}` (blocked while the agent is acting).

## Pairing and the cloud-phone Google account

With `APP_TOKEN` set, every API call needs a bearer token: the admin `APP_TOKEN`, or a device token from pairing. On start the service
prints a one-time pairing code (valid 10 minutes). State (paired devices as hashes, the linked account) is kept in `data/state.json`
(override with `STATE_FILE`), mode 600, gitignored.

| Endpoint | Purpose |
|---|---|
| `POST /api/pair {code, name}` | Exchange a pairing code for a device token (5 wrong codes lock pairing for 60 s) |
| `POST /api/pair/code` | Owner issues another pairing code |
| `GET /api/devices`, `DELETE /api/devices/{id}` | List and revoke paired devices |
| `GET /api/me` | Who is calling, the Google account on the cloud phone (or null) |
| `GET /api/cloud-account` | Reads the real account list from the phone (`dumpsys account`) and syncs it |
| `POST /api/cloud-account/start {consent:true, email?}` | Starts the sign-in run: the agent opens Add account, types the email, then hands off for the password |
| `DELETE /api/cloud-account?confirm=true` | Starts a run that removes the account (asks for approval first) |

The device is the source of truth: after a provisioning or removal run the server re-reads the phone's accounts and ignores what the model claims.

## Chat-first app (design v2, milestones 1 and 2)

Open http://127.0.0.1:8000. Left: bots (each a conversation). Centre: chat with a live status line while the phone works and action cards when the agent
needs you (approve, choose, answer, take over the phone, enter a code). Right: Details, Library (screenshots from finished tasks) and Mobile (live
thumbnail, Open for full screen, Take control, I'm done).

- Muse Spark 1.3 is the orchestrator (chat, memory, deciding when to use the phone). Muse Wonder is the phone worker only. See `agent/config.py`, `prompts/`.
- Tasks that need the phone run as background jobs on one shared phone (FIFO queue). Memory is SQLite full-text search (`data/app.db`).
- New endpoints: `/api/bots`, `/api/bots/{id}/state|messages|events|jobs|library`, `POST /api/items/{id}/answer`, `POST /api/jobs/{id}/cancel`,
  `GET /api/computer/state`, `POST /api/computer/control` (take, release).
- Not built yet: vault, sleep/wake, routines, voice, Android client (see docs/design-v2.md).
