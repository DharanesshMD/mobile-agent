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

## Run the service and web chatbot

```bash
uv run python -m agent.service        # http://127.0.0.1:8000, uses the emulator and .env
```

Set `APP_TOKEN` to require a bearer token (mandatory if `HOST` is not loopback). The web UI shows a live phone view, streams each
agent step, and shows cards when the agent needs you: approval (`ask`), a handoff to sign in yourself (`handoff`), or a one-time
SMS code (`request_otp`). When the agent is idle or paused you can click the phone view to tap, and type or scroll yourself.

API: `POST /api/runs`, `GET /api/runs/{id}/events` (SSE), `POST /api/runs/{id}/resume`, `POST /api/runs/{id}/cancel`,
`GET /api/device/screenshot`, `POST /api/device/{tap,type,key,swipe}` (blocked while the agent is acting).
