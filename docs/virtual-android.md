# Virtual Android for games: the hybrid plan (31 Oct 2026)

Problem: the official emulator is heavy, and some apps and games refuse to run on it (Clash of Clans kills itself at start-up). We want a second, light,
free "virtual Android" that behaves like an ordinary device for the agent, keep the emulator for what it does well (Google sign-in, Play Store), and keep the
real-phone app for what only real hardware can do.

## What the reference desktop agent teaches (design only, nothing copied)
Its cloud machine is not a special OS. It is a small Linux pod that hibernates when idle, with a command daemon (run tools), a VNC server streamed to the
browser through noVNC (the screen is just a remote framebuffer), and tunnels for network access. So "low quality like Grok Bot" is a minimal Linux box with a
streamed screen. The Android equivalent is Android running as a container: it shares the host kernel (no VM inside), `adb` is the command daemon, `screencap`
or scrcpy is the stream, and `docker pause` is hibernate.

## Options (researched 31 Oct, sources in the research notes)
| Rank | Option | Notes |
|---|---|---|
| 1 | Official emulator, tuned | Only option with host GPU on Apple Silicon. Keep it as the default phone. |
| 2 | Redroid in a Lima (`vz`) Ubuntu arm64 VM | Native arm64, light, headless, `adb` on a TCP port. Software rendering only. **Spike running.** |
| 3 | Redroid in OrbStack | Needs unmerged workarounds. Fallback. |
| 4 | Redroid in UTM/QEMU | GPU path has colour bugs. |
| 5 | Redroid on an arm64 cloud instance (Graviton, about $49/month t4g.large) | Native, no GPU, costs money. For later. |
| - | Cuttlefish, Android-x86, Genymotion | Need KVM, or translation, or are not free for this use. Not for an M1. |

Docker Desktop cannot run Redroid (its kernel has no binder). Apple's nested virtualization needs M3+, this Mac is an M1 Pro.
We do not spoof or hide the device identity. A game that still refuses to run on Redroid is reported as such, and the real-phone app is the answer for it.

## Architecture (the agent code stays the same)
The agent only needs an `adb` endpoint, so each virtual Android is a **phone** with a backend:
- `emulator` (exists): `EmulatorBackend`, serial `emulator-5554`.
- `redroid` (built): `LimaRedroidBackend` (Redroid in the Lima VM), serial `localhost:5565`, start/stop/pause/resume through `docker`, same `booted()`/`healthy()` contract.
- `real phone` (built): the Mobile Agent app, one bot per phone.
Per-bot phone assignment generalizes what the phone bot already does: a bot has a phone (shared emulator, games phone, or a real phone), jobs follow the bot.
The service passes an explicit serial per phone (one adb server, no "default device"). The Mobile panel shows the selected bot's phone.

## What is built (31 Oct 2026)
- `agent/virtual.py` `LimaRedroidBackend`: the contract `Lifecycle` uses (`status` running/paused/stopped, `start`, `stop`, `pause`, `resume`, `booted`, `healthy`), through
  `scripts/virtual-android/redroid.sh`. Sleep is `docker pause` (about 0.2 s). Stop is `redroid.sh shutdown`: the container AND the Lima VM (frees 1-2 GB of the Mac's RAM; a cold
  start is about 15-22 s, against about 5 s when only the container was stopped). Start runs in the background and `Lifecycle` polls `booted()`.
- Every adb call names its phone (`-s`): the shared emulator is `ANDROID_SERIAL` or `emulator-5554` (`default_serial`), the games phone `localhost:5565`. Two devices on one adb server
  no longer break each other. No second adb server.
- The Games phone has its own bot (kind `virtual`, `phone_id` `games`, created at start-up only when Lima and the `rd` VM exist; `GAMES_PHONE=off` disables it). Its jobs always run on it
  (`virtual:games`) and no other bot's job ever does. The first message states the limits plainly.
- Each phone is its own lane in `JobManager` (emulator, `virtual:games`, `phone:<id>`): one running job per lane, lanes run in parallel, `after` works across lanes. The shared Mobile
  panel shows the emulator lane; `?phone=games` shows the games phone (state, wake, sleep, live view). The games phone is watch only (no owner control).
- `scripts/virtual-android/install-apk.sh <file.apk>`: installs on the games phone (prints the sha256 first).

## Getting free games onto the games phone
There is no Google Play on it, so apps are APK files. Use F-Droid's official repo (https://f-droid.org/repo/), which lists a sha256 for every APK in `index-v1.json`:
```bash
curl -O https://f-droid.org/repo/com.shatteredpixel.shatteredpixeldungeon_922.apk
shasum -a 256 com.shatteredpixel.shatteredpixeldungeon_922.apk     # compare with the hash for that apkName in https://f-droid.org/repo/index-v1.json
bash scripts/virtual-android/install-apk.sh com.shatteredpixel.shatteredpixeldungeon_922.apk
```
Tested: Shattered Pixel Dungeon (libGDX) and Mindustry. A game that refuses to run on a virtual device is reported as such; the real-phone app is the answer for it.

## Plan
- [x] Research (this file), reference study of the desktop agent
- [x] Spike: Redroid in Lima, boot time, RAM, adb speed, pause/resume, a free game (scripts/virtual-android/, docs/virtual-android-spike.md)
- [x] `LimaRedroidBackend`, a "Games phone" bot with its own phone and live view, one job lane per phone so lanes run in parallel
- [ ] Tune the worker for games if the live test shows a need (see the live verification notes in docs/virtual-android-spike.md)
- [ ] If the games phone is not good enough for a game: tune the official emulator or use the real-phone app for it
- [ ] Demo: show the Mobile panel with the games phone running a free game, state limits honestly
