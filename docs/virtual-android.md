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
Per-bot phone assignment generalizes what the phone bot already does: a bot has a phone (shared emulator, Cloud phone 2, or a real phone), jobs follow the bot.
The service passes an explicit serial per phone (one adb server, no "default device"). The Mobile panel shows the selected bot's phone.

## What is built (31 Oct 2026)
- `agent/virtual.py` `LimaRedroidBackend`: the contract `Lifecycle` uses (`status` running/paused/stopped, `start`, `stop`, `pause`, `resume`, `booted`, `healthy`), through
  `scripts/virtual-android/redroid.sh`. Sleep is `docker pause` (about 0.2 s). Stop is `redroid.sh shutdown`: the container AND the Lima VM (frees 1-2 GB of the Mac's RAM; a cold
  start is about 15-22 s, against about 5 s when only the container was stopped). Start runs in the background and `Lifecycle` polls `booted()`.
- Every adb call names its phone (`-s`): the shared emulator is `ANDROID_SERIAL` or `emulator-5554` (`default_serial`), Cloud phone 2 `localhost:5565`. Two devices on one adb server
  no longer break each other. No second adb server.
- The Cloud phone 2 has its own bot (kind `virtual`, `phone_id` `games`, created at start-up only when Lima and the `rd` VM exist; `GAMES_PHONE=off` disables it). Its jobs always run on it
  (`virtual:games`) and no other bot's job ever does. The first message states the limits plainly.
- Each phone is its own lane in `JobManager` (emulator, `virtual:games`, `phone:<id>`): one running job per lane, lanes run in parallel, `after` works across lanes. The shared Mobile
  panel shows the emulator lane; `?phone=games` shows Cloud phone 2 (state, wake, sleep, live view). Cloud phone 2 is watch only (no owner control).
- `scripts/virtual-android/install-apk.sh <file.apk>`: installs on Cloud phone 2 (prints the sha256 first).

## Getting free games onto Cloud phone 2
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
- [x] `LimaRedroidBackend`, a "Cloud phone 2" bot with its own phone and live view, one job lane per phone so lanes run in parallel
- [x] Tune the worker for games (see docs/virtual-android-spike.md)
- [x] Hybrid routing (31 Oct): phone directory in every bot's context, `phone_check` with `on`, Master reroutes a failed handover (max 2, no repeats), a phone without Play refuses Play links and is never offered a Play install
- [x] `move_app` (Master): copy an installed app emulator <-> Cloud phone 2 without a store (APK files only, never data or sign-in); per-phone Google account fact; Google sign-in is a task on that phone's bot with the vault, never copied
- [ ] If Cloud phone 2 is not good enough for a game: tune the official emulator or use the real-phone app for it
- [ ] Demo: show the Mobile panel with Cloud phone 2 running a free game, state limits honestly


## Hybrid routing and sharing between phones (31 Oct 2026)
- `agent/phones.py`: the directory (capability, state, Google account fact, known-not-to-run apps from crash-stopped jobs) shown to every bot; Master also gets the routing rules.
- Master's `phone_check` takes `on` (emulator, games, a real phone's bot name), wakes a sleeping phone, and says honestly if one is offline.
- When a task Master handed over fails, Master gets a turn to move it to a phone that can do it (2 reroutes at most per request, never the same goal to a phone that already tried it), then explains.
- `move_app` (Master only): `pm path` -> `adb pull` each APK -> `adb install-multiple -r` on the other phone -> verified with the app_installed check -> temp files deleted. Only the app moves.
  Not for the owner's own phone (it installs from its own Play Store).
- Google sign-in: never copied between phones. A phone that can have an account (the emulator, the owner's phone) gets a sign-in task on its own bot (type_secret from the vault, owner approves, handoff for 2-step).
  Cloud phone 2 has no Google services and can never sign in.

## Apps that refuse to run: the automatic chain (1 Nov 2026)
When an app crashes (or will not stay up) on a phone, the agent tries the owner's other phones in order, in code (`agent/escalate.py`), with no question at each step:
the phones where it already stayed up first, then the shared emulator, Cloud phone 2, the extra adb phones, and last the owner's own phone (only while its app is connected; it installs
from its own Play Store through its bot). An app that is missing on an adb phone is copied from a phone that has it (`move_app`: only the app, never data or sign-in).
- At most `CHAIN_MAX` (4) phones, never the same phone twice, never a phone where the app is known not to run (`launch_facts` and crashes seen in tasks).
- After opening it the chain checks that the app is in front and has not crashed for 20 s, and describes the screen from a screenshot before saying it runs. It never taps, signs in or plays.
- What it learns is stored (`launch_facts`): the directory shows "Known to run here" / "Known NOT to run here", and the next "open it" goes straight to the phone that works.
- It works from any bot: the answer lands in the chat where the owner asked (for a task Master handed over, in Master's chat). It ends in one honest message when no phone can run it.
- Still asks the owner for: untrusted installs, signing in, secrets, deleting, buying.

## Extra adb phones (`ADB_PHONES`)
Any adb endpoint the owner manages: `ADB_PHONES="pixel=R5CN1234,cloud=10.0.0.5:5555"` (or `data/phones.json`: `{"pixel": "R5CN1234"}`). Each one that answers `adb -s <serial> get-state`
gets its own bot ("Pixel phone", kind `virtual`), lane, live view (watch only) and directory entry (status, installed apps, Google Play detected with `pm list packages com.android.vending`).
No power management: it is always on while adb reaches it, and a task on one that went offline fails honestly. The bot list re-checks them when it is polled. Serials that clash with the shared
emulator (`emulator-*`) or Cloud phone 2 (`localhost:5565`) and reserved names (`games`, `emulator`, ...) are refused. BlueStacks with adb enabled, a USB phone or a cloud phone all fit.


## Game account safety
The agent never plays games whose publishers ban bot accounts (Supercell, Niantic), on any phone; Cloud phone 2 and extra adb phones can be driven by the owner (Take control). See [game-safety.md](game-safety.md).
