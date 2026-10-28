# Local Android device (Mac, Apple Silicon)

Day-1 dev device: the official Android emulator, headless, controlled over ADB.
The agent layer only talks ADB, so this can later be swapped for Redroid or Cuttlefish on a cloud host.

## One-time setup (done on 27 Oct 2026)

SDK root: `/opt/homebrew/share/android-commandlinetools` (installed via Homebrew command-line tools).

```bash
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
sdkmanager --sdk_root=$ANDROID_HOME "emulator" "system-images;android-34;google_apis;arm64-v8a"
avdmanager create avd -n agent34 -k "system-images;android-34;google_apis;arm64-v8a" -d pixel_6
```

## Run

```bash
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
export PATH=$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH
emulator -avd agent34 -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect -no-snapshot &
adb wait-for-device   # then poll: adb shell getprop sys.boot_completed  (prints 1 when ready)
python3 spike/probe.py spike/out
```

First boot took about a minute. Device: Android 14, 1080x2400.

## Spike result

`spike/probe.py` observes (screenshot + UI tree) and acts (tap, key, intent launch) over ADB, verified on Settings.
Lesson for the agent loop: UI transitions lag, so wait and re-observe after every action before judging success.

## Notes

- Disk was 28 GB free at setup; the emulator and image use a few GB.
- `adb shell monkey` failed to launch Settings (exit 251). Launch by intent with `am start -a` instead.
- Cloud host (Redroid/Cuttlefish) is still unverified and deferred.

## Play Store emulator (added 27 Oct 2026)

For Google sign-in and installing apps. Image `system-images;android-34;google_apis_playstore;arm64-v8a` (about 2 GB download,
4 GB unpacked), AVD `agent34ps`. Has `com.android.vending` (Play Store) and Google Play services. Run only one emulator at a time
(memory and disk): the plain `agent34` AVD was shut down when this one was booted.

```bash
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
export PATH=$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH
emulator -avd agent34ps -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect -no-snapshot &
```

Use `-no-snapshot` for clean boots; drop it (and `-no-window`) if you want the emulator to keep its state, including a signed-in account,
between runs. Without a snapshot, a fresh boot may keep data on disk in the AVD folder (`~/.android/avd/agent34ps.avd`).

Verified: the agent opens the Play Store, recognizes "Sign in" and hands off to the owner (`handoff`) without signing in.

Signing in: start the service, ask the agent to "Open the Play Store and sign in", then in the web UI use the phone view to enter the
email and password yourself. Use a throwaway Google account, not your main one. If Google asks for an SMS code, that is the
`request_otp` flow once the phone client exists; until then type it in the phone view.

## Light profile (28 Oct 2026)

Start with `scripts/emulator.sh` (Mac GPU rendering, 3 cores, 2 GB, no window, clean boot). On an emulator the agent sets the screen to 720x1600 and turns animations off
when it starts (`PHONE_PROFILE=off` to disable; `Device.restore()` undoes it). Measured effect and the numbers are in docs/quality-log.md.
