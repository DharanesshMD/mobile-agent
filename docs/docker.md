# Run Mobile Agent in Docker

One image runs the web UI, the orchestrator and bots, the vault, replay, the A2A door and the phone gateway. Judges and reviewers can run it without Python, uv or an Android SDK.

## Quick start

```bash
cp .env.example .env                 # optional; put META_API_KEY in it for chat turns
docker compose up -d --build
docker compose logs mobile-agent    # prints the access link: http://localhost:8000/#token=...
```

Open the link. **A Setup window opens by itself** (also under "Setup" in the sidebar) and walks you through the rest, no files to edit:

1. **Model API key**: paste it; it is checked against the model API, saved in the data volume (`settings.env`, mode 600) and used at once. (A key in `.env` or `-e META_API_KEY=...` works too; one saved here wins.)
2. **This computer's Wi-Fi address**: Docker cannot see it, and a phone needs it. Type it (the window shows the command to find it).
3. **Android phone (emulator)** or **a real phone**: run the one command the window shows and it is detected; or press "Connect a phone" for the usual pairing window (QR code and code, same Wi-Fi).

The app refuses to listen on a non-loopback address without a token, and inside a container it must, so the entrypoint uses `APP_TOKEN` from `.env` or creates a random one once in the volume (`/app/data/.app_token`; read it with `docker compose exec mobile-agent cat data/.app_token`). Keep `#token=...` in the address, or paste the token into the page's Token box.

Without Compose:

```bash
docker build -t mobile-agent .
docker run -d --name mobile-agent -p 127.0.0.1:8000:8000 -p 8765:8765 -v mobile-agent-data:/app/data mobile-agent
```

Tests inside the image (no phone or key needed, about 4 minutes): `docker build --target test -t mobile-agent:test . && docker run --rm --entrypoint python mobile-agent:test -m unittest discover -s tests`

## What works in the container

| Part | In Docker |
|---|---|
| Web UI, bots, chat, memory, library, routines, replay, vault | Yes. Chat turns and phone tasks need `META_API_KEY` (or the open-weight settings) in `.env`. |
| A2A door (port 8766) | Yes, while you switch it on in the UI. A token is always required. |
| A real Android phone through the phone gateway ("Connect a phone") | Yes, over your Wi-Fi. See below. |
| A real phone over `adb` on the network (`ADB_PHONES`, `adb connect ip:5555`) | Yes. `adb` is in the image. |
| A real phone over USB | Not from Docker Desktop (macOS/Windows cannot pass USB through). Use the gateway, or run the app on the host. On Linux add `--device /dev/bus/usb` or point at the host's adb server with `ANDROID_ADB_SERVER_ADDRESS` (not tested here). |
| Phone (Android emulator) | **Yes, running on your computer (not in the container), with full hardware speed.** Docker cannot run an emulator (no KVM on Docker Desktop; Google has no Linux-ARM emulator at all). Instead run `bash scripts/host-emulator.sh` once in a terminal on the Mac/Linux host: it installs the SDK and emulator the first time (about 2.5 GB, needs Java, asks you to accept the Android SDK terms), creates the phone and starts it. The container reaches it through the host's adb server (`host.docker.internal:5037`, loopback only on the host) and **finds it by itself within seconds**: it saves the settings and restarts once. The Setup window shows the command and the progress. Windows hosts: run the script in WSL, not tested. |
| BlueStacks, Phone 2 (Lima + Redroid) | **No.** They need host virtualisation tools a container does not have (`GAMES_PHONE=off`). Run the app on the host to use them. |
| Building the Android app (`scripts/build_apk.sh`) | No. The image holds a Java runtime for the SDK tools but no Gradle or build tools. Build the APK on a machine that has them, then copy it in: `docker cp mobile-agent.apk <container>:/app/data/apk/mobile-agent.apk` (the container user must be able to read it). Without it the pairing page cannot offer a download, but an already installed app can still pair with the code. |

## Connecting a real phone

The door opens only when you press "Connect a phone", closes by itself (15 minutes, or 2 minutes after the last phone leaves), and serves nothing but the pairing page, the APK and the phone's own socket. Nothing in the image changes that; the published port simply has nothing listening until you open it.

Inside Docker the app sees only its bridge address, so the Setup window asks for your computer's Wi-Fi address (step 2; or set `PHONE_GATEWAY_ADVERTISE_IP` in `.env`). The gateway port 8765 is published on your LAN by default (`GATEWAY_BIND=127.0.0.1` closes that); nothing listens behind it until you press "Connect a phone". Press it, scan the QR code or enter the shown address and code in the phone app (automatic discovery does not cross Docker's network, so the address is typed or scanned).

The phone app file (APK): put a fresh `scripts/build_apk.sh` build at `docker/mobile-agent.apk` before `docker build` and the image carries it, so the pairing page can offer the download. Without it, install the app another way and pair with the code. HTTP only: use a Wi-Fi you trust. With `BIND_ADDR=0.0.0.0` the web UI is on the LAN too; the token protects it, so keep it private.

## Settings

| Variable | Default in Docker | Meaning |
|---|---|---|
| `META_API_KEY` (or the Setup window), `META_API_BASE`, `ORCHESTRATOR_BACKEND`, ... | from `.env` | See `.env.example`. `.env` is never copied into the image. |
| `APP_TOKEN` | generated, kept in the volume | Bearer token for every API call. |
| `BIND_ADDR` | `127.0.0.1` | Host address the ports are published on (compose only). |
| `APP_PORT`, `GATEWAY_PORT`, `A2A_PORT` | 8000, 8765, 8766 | Host-side ports (compose only). |
| `PHONE_GATEWAY_ADVERTISE_IP` | empty (Setup window) | LAN address shown to phones (see above). |
| `GATEWAY_BIND` | `0.0.0.0` | Host address the gateway port is published on (compose only). |
| `HOST`, `PORT`, `A2A_HOST` | `0.0.0.0`, 8000, `0.0.0.0` | Listen addresses inside the container. |

Ports: 8000 web UI and API, 8765 phone gateway, 8766 A2A. The container runs as user `app` (uid 10001), with a health check on `/api/health` and `restart: unless-stopped`.

## Data, upgrade and backup

Everything the app writes (database, logs, APK, vault key, runs, uploads) is in the named volume `mobile-agent-data`, mounted at `/app/data`. The vault key lives there too: a backup without it cannot open vault entries.

```bash
docker compose logs mobile-agent                         # app output
docker compose exec mobile-agent tail data/logs/errors.log
# upgrade: the volume is kept
git pull && docker compose up -d --build
# backup / restore
docker run --rm -v mobile-agent-data:/d -v "$PWD":/b alpine tar czf /b/mobile-agent-data.tgz -C /d .
docker run --rm -v mobile-agent-data:/d -v "$PWD":/b alpine sh -c 'cd /d && tar xzf /b/mobile-agent-data.tgz'
# start over (deletes all data)
docker compose down -v
```

The phone is not put to sleep by the container (that control needs the host): stop it with `adb emu kill`. If another copy of the app on the host also sleeps this emulator, it will hang adb calls until woken; the script wakes it. A harmless warning "could not apply the light profile yet" appears at start while no phone is connected.
