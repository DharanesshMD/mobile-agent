# Real-phone test: about one hour

Goal: run the Mobile Agent app on **your own Android phone**, pair it with the service on your Mac, and watch one task run on the phone's real screen.
Safest route: USB debugging plus `adb reverse`, so the server stays on `127.0.0.1` (nothing is exposed to the network, no `APP_TOKEN` needed).
Background and limits: [phone-client.md](phone-client.md). What to expect on screen: you see the phone act (it is not invisible).

## Route A (recommended): scan a QR code from the web app (about 10 minutes)
No cable and no typing. The phone downloads the app from your Mac over the Wi-Fi, with a one-time link.
1. Build the app file once: `bash scripts/build_apk.sh` (writes `data/apk/mobile-agent.apk` and prints its SHA-256).
2. Put the Mac and the phone on the same Wi-Fi, one you trust (the link is plain HTTP).
3. In the web app (http://127.0.0.1:8000), open the **Phones** panel, press **Connect a phone**, then **Allow phones on this Wi-Fi**. A QR code, a pairing code and the address appear.
4. Scan the QR code with the phone's camera. The page explains what the app can see and do and shows the file size and SHA-256. Press **Download the app**. Chrome warns twice because the link is plain HTTP ("can't be downloaded securely", then "might be harmful"): tap **Keep**, then **Download anyway** (the SHA-256 on the page is the file's fingerprint). Open the file and allow installs from your browser if Android asks.
5. Back on the page press **Open in the app**. The app shows the server and code already filled in: press **Confirm**.
6. In the app, turn on the accessibility service (Android 13 or newer: App info, three dots, **Allow restricted settings**, then try again) and allow background running.
7. The dialog on the Mac flips to "<your phone> connected" and closes. Your phone appears in the Phones panel and its own bot appears in the bot list.
8. In the app, **History** shows the tasks the assistant ran on this phone and everything it did here (time, operation, app; never screenshots or typed text). **Disconnect and revoke** makes the server forget the phone at once.
What opens on your network: one small door on port 8765 (`PHONE_GATEWAY_PORT`), only while you have it switched on. It serves only the page, the app file, pairing, the phone's own connection and its history; everything else answers 404. The pairing part closes 15 minutes after you open it, or 2 minutes after a phone pairs; a phone that is connected through it keeps working, and the door closes 2 minutes after the last such phone leaves. Use **Turn off** in the dialog at any time.
Then continue at step 7 (first task) below. Route B (USB, nothing exposed to the network) is the next section.

# Route B: USB with `adb reverse` (the rest of this page)

## Before you start (10 min)
- An Android phone, **Android 11 or newer**, unlocked, with a screen lock you can leave as is. Use a phone you can spare for a test. Do not test with banking or payment apps open, and do not point it at
  Supercell or Niantic games (the agent refuses to play them anyway).
- A USB cable that carries data. The phone and the Mac on any network (the app dials out through the cable).
- The service running **once** on the Mac (the Claude app starts it from `.claude/launch.json` on port 8000; check with `pgrep -fl agent.service`, there must be exactly one line). Check http://127.0.0.1:8000 loads.
- `adb` on your PATH in every terminal you use:
  ```bash
  export PATH=/opt/homebrew/share/android-commandlinetools/platform-tools:$PATH
  ```
- **Always pass `-s <serial>`** to adb. The emulator is also attached to the same adb server, and a bare `adb` command fails with "more than one device".
- **Never run `adb kill-server` while the service is running.** It drops the emulator connection and fails a running job.

## 1. Turn on USB debugging (5 min)
1. Settings > About phone > tap **Build number** 7 times (developer options turn on).
2. Settings > System > Developer options > turn on **USB debugging**. Also turn on **Stay awake** while charging for the test.
3. Plug the phone in, accept the "Allow USB debugging?" prompt on the phone (tick "Always allow from this computer").
4. On the Mac: `adb devices`. Your phone shows as `<serial>   device`. Note the serial; the emulator is `emulator-5554`.
   `unauthorized` means the prompt was not accepted: unplug, replug, accept it.

```bash
export SERIAL=<your serial>
```

## 2. Get the app onto the phone (5 min)
The debug build is at `android/app/build/outputs/apk/debug/app-debug.apk` (about 3.6 MB). If it is missing, build it (JDK 17, Android SDK with platform 35 and build-tools 35.0.0,
`android/local.properties` containing `sdk.dir=/opt/homebrew/share/android-commandlinetools`; the Gradle wrapper pins Gradle 8.11.1 and downloads it):

```bash
cd android && ./gradlew assembleDebug && cd ..
adb -s $SERIAL install -r android/app/build/outputs/apk/debug/app-debug.apk
```

If the install is refused ("blocked by Play Protect" or "install via USB"), accept the on-phone prompt; some phones need Developer options > **Install via USB** turned on.

## 3. Let the phone reach the server (1 min)
```bash
adb -s $SERIAL reverse tcp:8000 tcp:8000
adb -s $SERIAL reverse --list          # should list tcp:8000 tcp:8000
```
On the phone, `http://localhost:8000` now reaches the Mac's service. The reverse disappears if you unplug the cable or restart adb: run it again.

## 4. Pair (3 min)
1. On the Mac open http://127.0.0.1:8000, open Master (or any bot) > **Mobile** tab > **Get a pairing code** (or `curl -s -X POST http://127.0.0.1:8000/api/pair/code`). The code looks like `ABCD-EFGH` and is valid for 10 minutes.
2. On the phone open **Mobile Agent**. Server: `http://localhost:8000`. Code: the code. Tap **Pair**. The status line says "Paired with http://localhost:8000".
   (5 wrong codes lock pairing for 60 seconds.)

## 5. Allow it to operate the phone (10 min)
Android 13 and newer block accessibility for apps installed outside the Play Store until you allow restricted settings:
1. In the app tap **Open accessibility settings**, find **Mobile Agent** (under *Downloaded apps* or *Installed apps*) and try the switch. If it is greyed out or a "restricted setting" dialog appears, tap OK.
2. Back in the app tap **Open App info (restricted settings)**. Tap the **three dots** (top right) > **Allow restricted settings**. Confirm with your screen lock. (The menu item only shows after step 1 has been tried.)
3. Back to **Open accessibility settings** > **Mobile Agent** > turn it **on** > **Allow**. The app status shows "Accessibility service: on".
4. Tap **Allow background running (battery)** > Allow. On Samsung, Xiaomi, Oppo or OnePlus also set the app's battery usage to **Unrestricted** in App info.
5. Optional: **Allow display over other apps** helps opening apps from the background.
6. Allow notifications when asked (Android 13+).
7. Tap **Start agent**. A notification "Mobile Agent is connected" with a **Stop** button appears, and the app shows "Connection: connected".

## 6. Check that it is connected (2 min)
- In the web UI a new bot with a **Phone** tag appears within a few seconds (named after the phone model); its Mobile tab says **connected**. `curl -s http://127.0.0.1:8000/api/phones` lists it too.
- Tasks for the phone go **only** through that bot, and that bot never uses the emulator.

## 7. First task (5 min)
Open the phone's bot and send: **open Settings and tell me the first menu item**.
Expected: within a few seconds the phone's screen opens Settings by itself, the bot posts a status line ("Starting...", then step lines), and in about 10 to 30 seconds it answers with the first item
(for example "Network & internet"). You will see the phone move. Try a second one: "open Clock and tell me what tabs it has".

Do not touch the screen while it works (a touch can change what it sees). To stop: tap **Stop** in the notification, or **Stop agent** in the app, or say "stop" in the chat, or turn off the accessibility service.

## 8. What to capture for the video (10 min)
- The web UI with the Phone-tagged bot connected (screenshot), and the first task running (screen recording of the Mac, 10 to 15 s).
- The phone's own screen while it works: `adb -s $SERIAL shell screenrecord --time-limit 15 /sdcard/demo.mp4`, then `adb -s $SERIAL pull /sdcard/demo.mp4 .` (the recording is not tracked by git).
- Write down: time from sending to the answer, number of steps (shown in the job), anything odd.
- Make sure no personal notifications, contacts or accounts are visible on the phone when you record.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Bot says the phone is not connected | Open the app, check "Connection: connected"; tap Start agent; re-run `adb -s $SERIAL reverse tcp:8000 tcp:8000`; unlock the phone |
| Job fails with "the accessibility service is off" | Settings > Accessibility > Mobile Agent > on; redo step 5.2 if the switch is greyed out. It also turns off when the app is force-stopped |
| "Could not pair" | The code expired (10 minutes) or was typed wrong; get a new one. Check the server address is exactly `http://localhost:8000` and the reverse is listed |
| `adb devices` shows `unauthorized` or nothing | Unplug, replug, accept the prompt; try another cable or port. Do not run `adb kill-server` |
| `more than one device/emulator` | Add `-s $SERIAL` to every adb command |
| Phone goes to sleep and tasks stall | Developer options > Stay awake; keep it unlocked while testing; the agent cannot enter a PIN |
| The agent stops responding after a while | OEM battery saver killed the app: set battery to Unrestricted, lock the app in recents |
| Apps do not open from the background | Turn on "Allow display over other apps" in the app |

## Safety note
The agent acts on the **real screen** of your phone and taps what the task says. Stay near it, keep Stop within reach, and use a phone you control and low-risk tasks. It will not play Supercell or Niantic games and
it asks before sending or paying, but it is a debug build: treat it like a developer tool.

## Undo everything afterwards
```bash
adb -s $SERIAL shell am force-stop ai.mobileagent.client
adb -s $SERIAL uninstall ai.mobileagent.client
adb -s $SERIAL reverse --remove-all
curl -s http://127.0.0.1:8000/api/devices                 # find the paired device id
curl -s -X DELETE http://127.0.0.1:8000/api/devices/<id>  # revoke it
```
Also turn off USB debugging if you do not need it, and turn off Stay awake.

## Alternative: same Wi-Fi instead of USB
Only if you cannot use a cable. This exposes the service to everyone on that network, so it needs a token and a trusted network:
1. Stop the managed service (never run two copies), then start one yourself: `HOST=0.0.0.0 APP_TOKEN=$(openssl rand -hex 24) uv run python -m agent.service`. Keep the token; the web UI asks for it.
2. Find the Mac's address (`ipconfig getifaddr en0`). On the phone use `http://<that address>:8000` as the server. The rest is the same. Stop the service and restart it on `127.0.0.1` when you are done.

## If Play Protect blocks the install ("App blocked to protect your device")

**What it is.** Google Play Protect blocks an app that declares sensitive permissions (Accessibility, notification access, SMS) when it is installed from an
"internet-sideloading source": a browser download, a messaging app or a file manager. The dialog says "This app can request access to sensitive data" and has only
an OK button. Google's developer guidance (developers.google.com/android/play-protect/warning-dev-guidance) says the protection is active only in select markets.
Mobile Agent declares the Accessibility service (that is how it operates the screen), so a phone in such a market blocks the downloaded APK. It is not a finding about the app's code.

**What commonly works** (Google does not document exemptions, so none of this is a guarantee):
1. **Install from this computer** (recommended). In the web app: Connect a phone > "Phone says App blocked to protect your device?" > Install from this computer. It runs `adb install` on the listed phone,
   which is not a download. Over USB: turn on Developer options and USB debugging, plug in, tap Allow on the phone. Without a cable: Developer options > Wireless debugging > Pair device with pairing code,
   then type the address and 6 digits into the dialog (the phone and the computer on the same Wi-Fi). The dialog then tries to lift Android 13+ "restricted settings" for the app, hands it the pairing link
   (switch "Allow phones on this Wi-Fi" on first) and opens the Accessibility settings screen. It never turns the service on: you do, and you tap Confirm in the app to pair.
2. **Pause scanning for a minute.** Play Store > your profile icon > Play Protect > the gear > turn off "Scan apps with Play Protect". Install, then turn it back on. Some phones still hard-block.
3. If the dialog shows **More details**, choose **Install anyway**.
4. By hand: `adb install -r -d data/apk/mobile-agent.apk`.

**Xiaomi / MIUI.** `adb install` can fail with `INSTALL_FAILED_USER_RESTRICTED`. Settings > Additional settings > Developer options > turn on **Install via USB** (it needs a Mi account signed in and mobile data once);
if a confirm popup on the phone keeps cancelling the install, also turn off **MIUI optimization** there. Then Android 13+ may grey out the Accessibility switch: App info > three dots > Allow restricted settings.
MIUI also stops background apps: set Battery saver to No restrictions and turn on Autostart for Mobile Agent so it stays connected.

**Limits.** adb must be on this computer (`adb` in PATH, or ADB=/path/to/adb). Only the listed phones are touched (never the emulator or Phone 2), only our own APK is installed, and the pairing code is never logged.
Not verified on a connected phone in this repository's tests (they use a scripted adb); the Xiaomi steps come from reports of other users, not from a run here.
