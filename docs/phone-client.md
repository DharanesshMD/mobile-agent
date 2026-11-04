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
| `screen_state` | none | `{"interactive": bool, "locked": bool, "secure": bool, "hour": 0-23}`. Works with the screen off and without the accessibility service; wakes nothing |
| `wake` | none | the same four fields about 700 ms after waking the screen. NEVER unlocks (no PIN, no pattern, no dismissing the keyguard) |
| `notifications_recent` | `since_ms?`, `limit?` (1-50, default 20) | `{"enabled": bool, "reason": "" \| "off" \| "no_accessibility", "items": [{"app","pkg","title","text","at_ms","hidden"}]}` newest first (see "Notifications") |
| `otp_watch` | `seconds` (clamped 10-180), `since_ms?` | `{"enabled": bool, "watching": bool, "reason": "" \| "off" \| "no_accessibility" \| "notifications_blocked"}` (see "Verification codes") |
| `otp_poll` | none | `{"watching": bool, "remaining_s": int, "redacted": bool, "codes": [{"id","code","source","title","at_ms"}]}` |
| `otp_ack` | `id` | `{"ok": true}` |
| `otp_stop` | none | `{"ok": true}` |
| `push_file` (follow-up, not built) | `name`, `mime`, `data` (base64) | saves a picture the owner shared to Pictures/MobileAgent through MediaStore; needed for tasks that use a shared picture on a real phone |

## Reaching the server from a real phone

The phone dials out, so it only needs a route to the server: the same Wi-Fi (start the service with `HOST=0.0.0.0 APP_TOKEN=<long secret>`; it refuses to listen
on a non-loopback address without a token), or a private tunnel such as Tailscale (the phone gets the server's tailnet address, nothing is exposed publicly).
In the emulator the host is `http://10.0.2.2:8001`. Use https/wss for anything beyond a trusted network.

## Build (no Android Studio)

JDK 17, Android SDK in `/opt/homebrew/share/android-commandlinetools` (platform android-35, build-tools 35.0.0), **Gradle wrapper pinned to 8.11.1 with
AGP 8.9.x** (the installed Gradle 9.4.1 does not work with AGP 8). `cd android && ./gradlew assembleDebug`; install with `adb install -r`.

## First-run checklist (in the app)
The main screen starts with a live setup list. Each row says why it is needed, shows ✓ / ○ / ! , and its button opens the exact Settings screen; the list is re-checked when you come back from Settings and every 2 seconds. "Next step" jumps to the first row that is not done. Nothing is switched on for you.
| Row | Checked how | Button opens |
|---|---|---|
| Pair with your server | a device token is stored | the pairing fields (or scan the QR in the web app) |
| Allow restricted settings (Android 13+) | the `access_restricted_settings` app-op, and whether you already tried the accessibility switch; shown first (!) when the switch is greyed out | App info: three dots, Allow restricted settings |
| Turn on the accessibility service | our component is in `enabled_accessibility_services` | Accessibility settings |
| Allow notifications | runtime permission (Android 13+), else the app notification switch | the permission prompt, then the app notification settings |
| Run in the background | `isIgnoringBatteryOptimizations` | the battery-optimisation prompt |
| Display over other apps | `canDrawOverlays` (Android only lets an app start another app from the background with this) | the app's overlay screen |
| Xiaomi, Redmi, POCO only: Autostart, Pop-up windows in the background, Battery saver: No restrictions | cannot be read back: you tap Done | the MIUI screen (`com.miui.securitycenter` and `com.miui.powerkeeper`), falling back to App info with a one-line instruction |
| Optional: Let tasks fetch verification codes from this phone | the `otp_relay` switch (off by default); never blocks All set | toggles the switch (needs the accessibility service) |
| Optional: Let the assistant read my notifications | the `notif_read` switch (off by default); never blocks All set | toggles the switch (needs the accessibility service) |

When everything is ✓ the screen shows "All set" and the connection state. Permissions the app declares: INTERNET, FOREGROUND_SERVICE (+ special use), POST_NOTIFICATIONS, WAKE_LOCK, REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, QUERY_ALL_PACKAGES (the installed-apps requests) and SYSTEM_ALERT_WINDOW (background app launches); none of them is on Google Play Protect's blocking list. The one that is, Accessibility, is the app's purpose.

### Finding the computer (auto-detect)
The pairing part of the screen looks for the computer by itself when it opens, and on "Find my computer" / "Retry", so only the pairing code is typed. Step 1: a UDP question (`MOBILE_AGENT_DISCOVER v1`, port 8767) to the Wi-Fi's directed broadcast address and to 255.255.255.255, listened to for about 1.5 s; while "Allow phones on this Wi-Fi" is on, the computer answers with `{"app","v","port","name"}` and the address is the reply's source address. Step 2, if nobody answered (many routers drop broadcast): `GET http://<ip>:8765/api/hello` on every host of the phone's own /24 (300 ms timeout, 48 threads), accepting only a JSON reply with `app == "mobile-agent"`. One computer: its address fills the field ("Found <name>"); several: a list to pick from; none: "Could not find your computer. Is Allow phones on this Wi-Fi switched on...?" with Retry; no Wi-Fi: it says so and offers Wi-Fi settings. The address field stays editable ("Enter the address yourself"), a search never overwrites an address the owner typed, and the pairing code is never part of discovery. A `mobileagent://pair?server=...&code=...` link names the server, so it skips the search. The app needs only the normal permissions ACCESS_NETWORK_STATE and ACCESS_WIFI_STATE for this.

### "App blocked to protect your device" (Google Play Protect)
In some countries Play Protect blocks an app that declares Accessibility (also SMS and notification access) when it is installed from a browser, messenger or file manager, and the dialog has only OK. Install it from the computer instead, over adb (USB or Wi-Fi): `adb install -r data/apk/mobile-agent.apk`; adb installs are not "internet sideloading". Pausing "Scan apps with Play Protect" in the Play Store (profile, Play Protect, settings) also works on some phones. On Xiaomi, adb installs need Developer options, USB debugging and "Install via USB" (a Mi account is required) and, on some MIUI versions, turning off MIUI optimisation.

## Screen state and wake (locked phones)
`screen_state` reads `PowerManager.isInteractive`, `KeyguardManager.isKeyguardLocked` and `isDeviceSecure` plus the phone's local hour, so the assistant can tell "the phone is asleep" from "the phone is locked". `wake` turns the screen on with the wake-lock approach the app already uses; where that is not enough (some MIUI builds) it also starts `WakeActivity`, a see-through activity that asks for the screen to turn on and to show above the lock screen, then closes itself after 0.4 s. Neither op asks for the PIN, a pattern or a fingerprint, and neither calls `requestDismissKeyguard`: **the phone stays locked**. It needs no new permission (`WAKE_LOCK` and the overlay permission are already used; on Xiaomi the existing "Pop-up windows in the background" row also lets this activity start).
Owner-facing note: *The phone can wake and show what its lock screen shows. It can never unlock the phone. Whether notification text is visible while locked depends on the phone's lock-screen notification setting (Settings > Notifications > Lock screen), and on MIUI also on Notifications > Show on lock screen.*

## Notifications ("what did I miss")
Optional checklist row **Let the assistant read my notifications** (default off, `Prefs` flag `notif_read`). When on, the accessibility service (config: `typeNotificationStateChanged`, nothing broader) keeps the notifications that arrive in memory for `notifications_recent`:
- At most 50 entries and 24 hours, memory only (never written to disk), wiped when the service stops, when the owner switches the row off, and on app data clear. A repeated update of the same notification is stored once (an event carries no notification id or tag, so an update is recognised by app + title + post time, or by identical words within 5 s).
- **Verification codes are replaced by `[code]`** before a text is kept (the same finder as the code relay). Titles are cut to 80 characters and texts to 300. Our own notifications and ongoing foreground-service or media entries are skipped (a call or timer is kept).
- `hidden: true` means the phone gave only a placeholder ("Sensitive notification content hidden", a public version, or nothing while locked): the entry names the app but has an empty text.
- **A second source for locked phones.** On the Android 14 emulator, with a secure lock (PIN) set, notification events arrive **without their text** even when the screen is on; the lock screen itself, however, shows the text and is readable through the accessibility tree. So `notifications_recent` and `otp_poll` also read the notification rows that are visible on the lock screen or in the open shade (standard `android:id/title|text|big_text|app_name_text` and SystemUI's `notification_title|notification_text` ids; best effort, a maker's own layout may not match). This works with the screen off or on, never changes the screen, and entries read this way have `pkg: ""` and `at_ms` = the time they were read. Rows already showing when a code watch opens are ignored (their codes are old).
- The Ops log says only `notifications_recent: 7 items`; texts are never logged.

## Verification codes (the OTP relay, Android side)
Use case: a cloud phone's task needs a one-time code that was sent by SMS to the owner's real phone. The server opens a watch window, finds the code, asks the owner to approve it, and types it on the cloud phone. **The app declares no SMS or notification-listener permission** (those are exactly what makes Play Protect hard-block a sideloaded app): it uses the accessibility service it already has.
- `otp_watch {"seconds": 60}` opens (or extends, never beyond 180 s from now, never shortening) the window and shows a visible ongoing notice "Watching for a verification code for your assistant (N min)" with a **Stop** button (the code is never in it). Nothing is read outside a window. Refused with `enabled:false` and a reason when the owner's switch (checklist row **Let tasks fetch verification codes from this phone**, default off, `otp_relay`) is off, the accessibility service is not running, or notifications are blocked (no visible notice, no watching).
- `otp_poll` returns every candidate still held and not yet acknowledged, newest first, at most 5 (so a lost reply loses nothing; the server de-duplicates by `id`). `id` is 8 random hex characters; `source` is the notification's app label; `title` the sender/title cut to 40; `at_ms` an epoch time in ms. `redacted: true` means a notification arrived during the window but its text was hidden (lock screen, Android 15 redaction): the server should say "Your phone hid the message text; open Messages or type the code".
- `otp_ack {"id"}` forgets one candidate. `otp_stop`, the Stop button, the timer, the service stopping and the connection closing all close the window, remove the notice and **delete every candidate**.
- **Finding a code** (`OtpExtract`): a standalone 4-8 digit number (or 3+3 split by a space or hyphen, e.g. `G-123456`) near a cue word (code, OTP, verification, passcode, PIN, one-time, sign in, and several languages), or the only number in a message-style notification that has a cue. Years (unless the cue is right in front), amounts with a currency, times, dates, phone numbers and reference numbers without a cue are rejected. The table of cases runs on the phone: `adb shell am broadcast -n ai.mobileagent.client/.DebugReceiver --es action otp_selftest` logs `otp extractor pass 36/36`.
- **Privacy rules:** codes live only in memory (max 5, max 10 minutes). No digit is written to Ops logs, Logcat, `SharedPreferences`, files or any notification; the history says only `otp_watch 120 s` and `otp_poll: 1 candidate`; the History screen cannot show a code. The debug receiver's `otp_diag` prints counters only.
- Delivery of a notification event can take a few seconds on a busy phone: poll every 1-2 s for up to the window.

## Plan

- [x] Server: `PhoneHub`, `RemoteDevice`, `/api/phone/ws`, `/api/phones`, a persistent bot per phone, jobs routed by bot (11 tests)
- [ ] Android app: pairing, foreground service + socket, accessibility ops, notification with Stop (android/)
- [ ] Verify on the emulator end to end: pair, enable the service, run a real task through the chat on the "phone" target
- [x] Web: PHONE tag on the bot, status panel (connected / not connected) with a pairing code, pairing card on the shared bots
- [ ] Real phone: install the APK, same network or a tunnel, try a task
- [ ] Phase 2 (spike): invisible work on a virtual display through Shizuku
