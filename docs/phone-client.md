# Mobile Agent app for a real Android phone (30 Oct 2026)

Why: some apps (Clash of Clans and other games, banking apps) detect the emulator and refuse to run, and the emulator is slow on a busy Mac.
A real phone has none of those problems. The app on the owner's phone lets the server's agent operate that phone.

## Feasibility verdict (researched against AOSP android15-release, scrcpy, Shizuku, Open-AutoGLM)

| Question | Verdict |
|---|---|
| Can an app on an unrooted phone read the screen, tap, swipe, type, press back/home, take screenshots? | **Yes**, with an `AccessibilityService` (tree via `rootInActiveWindow`, `dispatchGesture`, `ACTION_SET_TEXT`, `performGlobalAction`, `takeScreenshot` on API 30+ with `canTakeScreenshot`). |
| Can it keep a connection to the server in the background? | **Yes**: foreground service + outbound WebSocket (works behind any router or firewall), battery-optimisation exemption. |
| Can it work **invisibly** (the owner keeps using the phone)? | **Not in the MVP.** An accessibility service acts on the real screen, so the owner sees it happen. Invisible work needs a `VirtualDisplay` plus `am start --display` / `input -d`, which needs the shell uid (Shizuku over wireless debugging, restarted after every reboot) and breaks on some apps and ROMs. Phase 2 spike. |
| Can it unlock a PIN or biometric lock? | **No.** The phone must be unlocked (or have no lock) when a task starts. |
| Secure screens (banking, passwords)? | Screenshots return a black frame (`FLAG_SECURE`); the app reports "secure screen" and the agent asks the owner to take over. |
| Will it pass Google Play review? | **No**: Play forbids autonomous accessibility agents. Distribute the APK directly (fine for the hackathon). |
| Android 13+ sideload restriction | The owner must open App info > menu > "Allow restricted settings" before the accessibility service can be enabled. The app guides them. |

What the MVP gives the owner: tasks that cannot run on the emulator run on their own phone, driven through the app, with a persistent notification
("Mobile Agent is controlling this phone" + Stop) and a kill switch. It is not a remote-control view: no screen streaming, no mirrored UI.

## Architecture

```
Mobile Agent app (phone)  ──outbound WebSocket──▶  server  /api/phone/ws?token=<device token>
  AccessibilityService  (does the taps, reads the tree, takes screenshots)
  Foreground service    (holds the socket, reconnects, keeps the app alive)
  MainActivity          (pair with a code, enable the service, battery exemption, status, Stop)
server:  agent/remote.py  PhoneHub (sockets)  +  RemoteDevice (same methods as agent.device.Device)
         each phone gets its own persistent bot (kind "phone", created on first connect, kept across reconnects);
         every job of that bot runs on that phone, and no other bot's job ever does (the target comes from the bot in JobManager.submit)
```

The phone's bot is a normal chat bot (memory, routines, queue, verification) whose phone is the real phone. Its tasks never fall back to the emulator: if the phone is
offline the bot says so and asks the owner to open the app. Master can hand a task to it with `delegate`; other bots are told it exists and cannot use it.

Pairing reuses the existing flow: the web app shows a code (`POST /api/pair/code`), the phone exchanges it at `POST /api/pair {"code","name"}` for
`{"device_id","token"}`, then connects with that token.

## Protocol (JSON text frames)

Phone → server, first frame: `{"type":"hello","name":"Pixel 8","model":"Pixel 8","sdk":34,"size":[1080,2400]}`; then `{"type":"ping"}` every 25 s
(the server drops a phone that is silent for 90 s; the app reconnects with backoff 1, 2, 4 ... 30 s).

Server → phone: `{"id":7,"op":"tap","args":{"x":540,"y":1200}}`. Phone → server: `{"id":7,"ok":true,"result":...}` or `{"id":7,"ok":false,"error":"..."}`.

| op | args | result |
|---|---|---|
| `size` | none | `[width, height]` in pixels of the current orientation |
| `screenshot` | none | base64 PNG at full resolution (black frame of the right size on a secure screen) |
| `elements` | `limit` | `{"keyboard": bool, "elements":[{"label","center":[x,y],"clickable","state"}]}` (same filtering as `Device.elements`) |
| `tap` | `x`,`y` | null |
| `swipe` | `x1`,`y1`,`x2`,`y2`,`ms` | null |
| `type` | `text` | null (appends to the focused field) |
| `key` | `name` (`KEYCODE_BACK`, `KEYCODE_HOME`, `KEYCODE_APP_SWITCH`, `KEYCODE_ENTER`, `KEYCODE_DEL`) | null |
| `open_app` | `package` | null |
| `open_url` | `url` | null |
| `open_intent` | `action` (an Android action string) | null |
| `launcher_apps` | none | package names that have a launcher icon |
| `packages` | none | all installed package names |
| `foreground` | none | package of the app in front |
| `crashes` | none | `[]` for now |
| `push_file` (follow-up, not built) | `name`, `mime`, `data` (base64) | saves a picture the owner shared to Pictures/MobileAgent through MediaStore; needed for tasks that use a shared picture on a real phone |

## Reaching the server from a real phone

The phone dials out, so it only needs a route to the server: the same Wi-Fi (start the service with `HOST=0.0.0.0 APP_TOKEN=<long secret>`; it refuses to listen
on a non-loopback address without a token), or a private tunnel such as Tailscale (the phone gets the server's tailnet address, nothing is exposed publicly).
In the emulator the host is `http://10.0.2.2:8001`. Use https/wss for anything beyond a trusted network.

## Build (no Android Studio)

JDK 17, Android SDK in `/opt/homebrew/share/android-commandlinetools` (platform android-35, build-tools 35.0.0), **Gradle wrapper pinned to 8.11.1 with
AGP 8.9.x** (the installed Gradle 9.4.1 does not work with AGP 8). `cd android && ./gradlew assembleDebug`; install with `adb install -r`.

## Plan

- [x] Server: `PhoneHub`, `RemoteDevice`, `/api/phone/ws`, `/api/phones`, a persistent bot per phone, jobs routed by bot (11 tests)
- [ ] Android app: pairing, foreground service + socket, accessibility ops, notification with Stop (android/)
- [ ] Verify on the emulator end to end: pair, enable the service, run a real task through the chat on the "phone" target
- [x] Web: PHONE tag on the bot, status panel (connected / not connected) with a pairing code, pairing card on the shared bots
- [ ] Real phone: install the APK, same network or a tunnel, try a task
- [ ] Phase 2 (spike): invisible work on a virtual display through Shizuku
