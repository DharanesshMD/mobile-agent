# Spike: BlueStacks Air as a fourth phone (31 Oct 2026)

Question: can BlueStacks Air (free, Apple Silicon) be an adb-controllable phone for the agent?
Answer: **not yet, and probably not worth it.** It installs and runs, ships with Google Play, but its adb shell is locked down until the owner turns ADB on in its Settings,
so tap, UI tree and package listing did not work. I could not turn the setting on myself (the owner interrupted my computer-use clicks), so control is **untested**.
Nothing here changes the device identity; BlueStacks ran exactly as it comes. No Supercell or other store game was launched or automated.

## Official source and signature
- Official page: https://www.bluestacks.com/mac (its button calls `cloud.bluestacks.com/api/getdownloadnow`, which redirects Macs to the build server).
- File: `BlueStacksInstaller_5.21.790.7505.pkg`, 1,029,855,185 bytes (982 MiB), from
  `https://ak-build.bluestacks.com/public/app-player/mac/nxt_mac2/5.21.790.7505/426aabd288b04f58a8200b751e9bcbcf/BlueStacksInstaller_5.21.790.7505.pkg`.
  SHA-256 `137accd707aa21028abf57412820d803fe550b07a1d5110ada291b80e10877aa`.
- Installer: signed by **Developer ID Installer: now.gg, Inc. (Team ID UJYFHY4XNR)**, "Notarized Developer ID", accepted by `spctl`, trusted timestamp 9 Sep 2026.
- App bundle (`com.now.gg.BlueStacks`, 5.21.790, arm64, hardened runtime): `codesign --verify --deep --strict` valid, same Team ID, notarized, accepted by `spctl`.
- Contents worth knowing: it bundles `qemu-system-aarch64`, `qemu-img`, an EDK2 firmware, a `Root.qcow2` (1.8 GB) system disk, `hd-adb` and an "AI Runtime" helper app.
  So it is a QEMU/HVF Android VM like the official emulator, with its own device identity.

## Install
- The `.pkg` needs an administrator password, which I cannot type. Instead I expanded it without installing (`pkgutil --expand-full`), verified the app bundle, and copied it with `ditto`
  to `/Applications/BlueStacks.app` (the user is in the admin group, no password needed). The package's pre/post-install scripts did not run (they log to `/var/log`, relaunch the app,
  make `Root.qcow2` world-writable, set up symlinks). The first launch (`open -g`) **crashed** (SIGSEGV in `utlDataDir()` / `runCommand`, report in `~/Library/Logs/DiagnosticReports`), probably because
  the data directory did not exist yet; the second launch worked and created `/Users/Shared/Library/Application Support/BlueStacks`.
- To install the supported way instead: double-click the `.pkg` and enter the Mac password.

## What I measured (this Mac: M1 Pro, 16 GB, already running the emulator and the Lima VM)
- Memory: first launch to a usable home screen about **87 s** (includes first-run setup). Default allocation is **4 cores and 4 GB RAM** (Settings > Performance: "High (4 Cores)", "Enhanced (4 GB)").
  The Mac's swap use grew by about 2 GB while it ran (6.9 to 9.0 GB of 10 GB) and free memory jumped by about 2.1 GB when I closed it. On this machine it is too heavy to run next to the emulator and the games phone.
- Screen: the home screen is BlueStacks' game store with promotions and ads (I did not click anything). It needs a visible window; I found no headless mode.
- Pause: `kill -STOP` freezes it (adb hangs), `kill -CONT` resumes it and adb answers again in **0.6 s**. Memory stays resident while paused.
- Quit: `osascript ... quit` shows a confirmation and returned "User cancelled"; `kill -TERM` closed it cleanly in under 25 s, and port 5555 was released.
- It shipped with 107 packages including **Google Play (`com.android.vending`), Google Play services, Chrome**, its own launcher and game store; no Google account signed in (the owner would have to sign in).

## adb
- Config file `/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf` (read only by me): `bst.enable_adb_access="0"`, `bst.enable_adb_remote_access="0"`,
  `bst.instance.Tiramisu64.adb_port="5555"`, `ram="4096"`, `cpus="4"`, `enable_root_access="0"`. The instance is Android 13 (arm64).
- BlueStacks' own support page for BlueStacks 5 says: Settings > Advanced > turn on Android Debug Bridge, Save changes; the port is shown on that tab
  (https://support.bluestacks.com/hc/en-us/articles/23925869130381-How-to-enable-Android-Debug-Bridge-on-BlueStacks-5). I could not confirm the same path in BlueStacks Air (its Settings sidebar is not
  readable through accessibility and my clicks were interrupted). Settings tabs seen: Performance, Display, Graphics, Preferences, Phone, User data, Advanced, About.
- Even with ADB access **off**, BlueStacks listens on `*:5555` (all interfaces). The official emulator uses `127.0.0.1:5555`, so `adb connect 127.0.0.1:5555` reaches the emulator, not BlueStacks. I reached BlueStacks with
  `adb connect <the Mac's LAN IP>:5555`. That also means other devices on the same network can reach that port: block it with the firewall if you use BlueStacks.
- With ADB off the shell is restricted. Working: `getprop`, `dumpsys`, `logcat`, `exec-out screencap -p`. Refused with `error: closed`: `shell echo`, `pm`, `wm`, `input`, `uiautomator`, `ls`, `settings`, `sh -c`.
  So the repo's `Device` class partly worked: `screenshot()` 0.57 s (raw screencap 0.6 s when idle, up to 5 s while the Mac was under memory pressure; 1080x1920 PNG about 2.1 MB),
  `foreground()` ok (`com.bluestacks.gamecenter`), `elements()` returned 0 (uiautomator refused), `size()` / `packages()` / `orientation()` failed, and `tap` is blocked. Whether the full shell opens after the ADB toggle is **not verified**.
- Device identity as it comes (observation only, nothing copied or changed): `ro.product.model=SM-G998B`, manufacturer samsung, `ro.hardware=exynos2100`, `ro.build.type=user`, no `qemu` properties.

## What the owner must do by hand
1. Open BlueStacks Air, Settings > Advanced, turn on Android Debug Bridge, Save (it may need a restart), and note the port.
2. Sign in to Google in the Play Store if apps are to be installed from Play.
3. Allow the macOS firewall to block incoming connections to port 5555, or leave BlueStacks closed when not in use.

## Verdict
**Not worth adding as a fourth backend now.**
- It is heavy (4 GB guest, needs a window) on a Mac that already runs the emulator and the games phone, and I found no headless or command-line control beyond `open`, `SIGSTOP` and `SIGTERM`.
- Control is unproven: the adb shell is restricted unless the setting is on, and I could not test the setting.
- It does not add what the emulator lacks for the agent's tasks (the emulator already has Play); its main appeal is that its device identity differs, which we do not copy, and
  a game that automates badly or violates a publisher's terms is still the owner's risk.
- If the owner enables ADB later and wants to try: a small `MacAppBackend` (start `open -a`, pause `SIGSTOP`, resume `SIGCONT`, stop `SIGTERM`, serial `<LAN IP>:5555`) and a bot, as for the games phone. About 2 hours plus tests.
