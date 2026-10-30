# Spike: Redroid (Android in a container) in a Lima VM on the Mac (31 Oct 2026)

Question: can a free, light "virtual Android" (Redroid inside a Lima Ubuntu arm64 VM) be a second phone for the agent, one that runs ordinary games?
Answer: **yes for ordinary apps and games, measured below.** It was not tried with Clash of Clans, Google Play or Play Integrity.

Machine: Apple M1 Pro, 16 GB, macOS 27 (no nested virtualization). Everything was free software. The service on port 8001, its emulator and the shared adb server
(5037) were not touched; the spike used its own adb server on 5038 (since stopped).

## What was built
`scripts/virtual-android/`
- `rd.yaml`: the Lima VM (Apple Virtualization.framework, Ubuntu 24.04 arm64, 4 vCPU, 4 GiB, 30 GiB disk, no Mac folders mounted, adb port 5565 forwarded).
- `setup.sh`: one-time setup (installs Lima through Homebrew if missing, creates the VM, installs the binder kernel module and Docker, pulls the image). Safe to re-run.
- `redroid.sh start|stop|pause|resume|status|adb`: runs the Android 13 arm64 container (`redroid/redroid:13.0.0_64only-latest`, 720x1280 at 320 dpi, only the documented
  display flags) and connects adb at `localhost:5565`. Port 5565, not 5555: on the Mac 5555 is the official emulator's own adb port.

## What worked
- **binder on Lima's kernel (6.8.0-147-generic): yes, with one package.** binder is a module in Ubuntu's generic kernel and ships in `linux-modules-extra-$(uname -r)`.
  `modprobe binder_linux devices=binder,hwbinder,vndbinder` loads it. Two files make it survive reboots (`/etc/modules-load.d/redroid.conf`, `/etc/modprobe.d/redroid.conf`).
  After a kernel upgrade, reinstall that package. The container ran with `--privileged` and `androidboot.use_memfd=1` (no ashmem on this kernel). The DKMS fallback was not needed.
- **Boot:** `docker run` to `sys.boot_completed=1` in **5.5 s** (empty /data); `docker start` after a full VM restart 5 s. VM start 10–13 s. A cold start from "VM off" to a usable phone is about **15 s**
  (the official emulator takes about a minute).
- **adb works as is:** the repo's `agent.device.Device('localhost:5565')` ran unchanged: `size()` 720x1280, `is_emulator()` false, `screenshot()` 94 ms, `packages()` 103, `foreground()` correct, `tap()` ok.
  The uiautomator2 screen reader installed itself and works (first call 0.96 s, then **22 ms**). `uiautomator dump` through adb takes 2.0 s (it waits for the app to idle).
- **Screenshot speed (`adb exec-out screencap -p`, 720x1280):** 379 ms on the launcher (650 KB), **100 ms** inside a game (x10 average each).
- **pause / resume:** `docker pause` 0.14 s, `docker unpause` 0.21 s. adb answered **immediately** after unpause on the same connection and the game process survived. During a pause adb calls hang (as with SIGSTOP).
- **Games (F-Droid, sha256 verified against F-Droid's index, source https://f-droid.org/repo/):**
  - Shattered Pixel Dungeon 4.0.2: `com.shatteredpixel.shatteredpixeldungeon_922.apk`, 30,275,058 bytes. Launches, shows the intro, the hero picker and full-screen artwork,
    starts a run, loads the dungeon level and renders it with animated water; input (taps) works. No crashes.
  - Mindustry 8-fdroid-160.7: `io.anuke.mindustry_1115.apk`, 78,182,684 bytes. Menu renders with an animated background (1–2 % of pixels change between shots 0.3 s apart) and taps work. No crashes.
- Rendering is software only (ANGLE on Vulkan SwiftShader, on the CPU). Screenshots look correct. Frame rate was judged only from screenshot-to-screenshot change, not measured with a frame counter.

## Resources (measured)
| | RAM | CPU |
|---|---|---|
| Android container idle (launcher) | 600–615 MiB | under 1 % of a core |
| Shattered Pixel Dungeon running | 740 MiB | 17–107 % of one core (loading is the heaviest) |
| Mindustry running | 1.68 GiB | about 105 % of one core |
| The Lima VM process on the Mac (4 GiB guest) | about 1 GB resident when idle, up to about 2 GB after a game (includes disk cache) | |

Docker Desktop's own VM (about 440 MB) was also running during the spike; quitting Docker Desktop frees that. Memory pressure stayed fine (free 33–50 %).

## What does not work, or was not tested
- **No Google Play, no GApps.** Apps go in by `adb install`. Signing in to a Google account, Play Store installs and Play Integrity were not tried (Redroid needs MicroG/OpenGApps and the Android ID
  registered with Google; the `redroid-script` project builds those images, unverified here).
- **Games expose no UI tree** (they draw into one GL surface): `Device.elements()` returned 0 elements. The worker has to work from screenshots and coordinates, the same as on the emulator.
- **The device identity is plainly Redroid:** `ro.product.model=redroid13_arm64_only`, `ro.hardware=redroid`, `userdebug` build, Android 13. Nothing was changed or hidden. A game that checks for it can
  still refuse; **Clash of Clans was not tested**, and no claim is made about it.
- Not tested: the AWS x86 box, Graviton, Android 14 images, GPU modes other than software, long sessions, many games.

## Verdict
Good enough to be a second, "games and heavy apps" phone: boots in 5 s, pauses and resumes instantly, uses less RAM than the emulator (0.6 GiB idle), exposes the same adb endpoint, and ran two ordinary OpenGL games
with correct output. It does not replace the official emulator for anything that needs Google Play or Google sign-in, and it will not be faster at graphics (software rendering, though the 720x1280 size keeps it playable).
Recommended: keep the emulator as the main phone and add this as the games phone behind the same `Device` interface.

## Plugged into the agent (done, 31 Oct 2026)
- `agent/virtual.py` `LimaRedroidBackend` (same contract as `EmulatorBackend`, calls `scripts/virtual-android/redroid.sh`: `state`, `start`, `pause`, `resume`, `shutdown`).
- A second `Device("localhost:5565")` with its own `Lifecycle`, a "Games phone" bot (kind `virtual`), jobs routed by bot, one job lane per phone, a live view and
  sleep/wake per phone in the Mobile tab. The screen reader, the worker, the queue and the checks needed no change. Details: docs/virtual-android.md.

## Live verification through the chat (31 Oct 2026, real models)
Games phone woken through the API: **22 s** from "off" (Lima VM stopped) to "on". Shattered Pixel Dungeon installed with `install-apk.sh` (sha256 matches F-Droid's index).
- **Lanes in parallel:** one message to the Games phone bot (the game) and, 2 s later, one to an emulator bot ("open Settings and tell me the Android version"). The emulator job ran 3-24 s and
  finished (Android 14) while the game job ran 0-27 s: both were running at the same time.
- **First run failed for a plain bug, not the game:** `Device.open_app` used `monkey`, which exits 251 on Redroid even though the app is installed. Fixed: when monkey fails or finds nothing, resolve the
  launcher activity (`cmd package resolve-activity`) and `am start -n` it (tests in tests/test_virtual.py).
- **Run 2 (open, intro, hero pick, dungeon):** the game opened and the worker reached the first dungeon level in 7 steps, then kept exploring and hit the 25-step budget. It repeated the same tap on the
  intro for many steps (animated screens never look "unchanged" to a byte comparison).
- **Two changes for games:** (1) `settle` gave up after 4 s on every step because a game never stops animating; after two timeouts in a row the wait is cut to 1.2 s (average settle 2.4 s to 1.3-1.7 s,
  step about 8 s to about 5-6 s); (2) on such an animated screen "unchanged" is judged on a coarse grey picture, so a repeated tap that changes only the sprites gets the "change approach" warning;
  (3) one worker-prompt rule: in a game, do exactly what the goal asks (a few moves, one level) and finish with `done` describing the screen.
- **Run 3 (final, same message):** `done` in **56 s, 23 steps**: new Warrior run, depth 1, full HP, the starting room described correctly (walls, west door, stairs, grass, water, the Warrior's position), and the
  prompt text read from the screen. The chat reply was accurate. It still repeated the intro tap about 5 times and tapped the hero portrait several times before the run started, so it plays sensibly but not
  efficiently; no UI tree means coordinates from the screenshot.
- Resources: see the table above; software rendering kept the Mac responsive. Screenshots of the live view show the game in the Mobile tab of the Games phone bot.

