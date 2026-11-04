#!/usr/bin/env bash
# Install (first time) and start the Android emulator on THIS computer, for the Mobile Agent running in Docker.
# The container reaches it through this computer's adb server and finds it by itself: no other step.
# Needs: Java 17+ (only for the first install), about 3 GB of disk, curl and unzip. Mac or Linux.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AVD="${AVD:-agent34ps}"

# an SDK that is already here (Homebrew, Android Studio, or one this script installed earlier)
SDK="${ANDROID_HOME:-}"
for c in "$SDK" /opt/homebrew/share/android-commandlinetools "$HOME/Library/Android/sdk" "$HOME/Android/Sdk" "$HOME/.mobile-agent/android-sdk"; do
  [ -n "$c" ] && [ -x "$c/emulator/emulator" ] && SDK="$c" && break || SDK=""
done

case "$(uname -m)" in arm64|aarch64) ABI=arm64-v8a ;; *) ABI=x86_64 ;; esac
IMG="system-images;android-34;google_apis_playstore;$ABI"

if [ -z "$SDK" ]; then
  SDK="$HOME/.mobile-agent/android-sdk"
  command -v java >/dev/null || { echo "Java 17+ is needed once to install the Android tools. Mac: brew install --cask temurin   Linux: sudo apt install default-jre-headless"; exit 1; }
  echo "This will download the Android SDK tools, the emulator and an Android 14 Play Store image (about 2.5 GB) into $SDK."
  echo "By continuing you accept the Android SDK terms: https://developer.android.com/studio/terms"
  read -r -p "Press Enter to accept and continue, Ctrl-C to cancel " _
  case "$(uname -s)" in Darwin) OS=mac ;; *) OS=linux ;; esac
  mkdir -p "$SDK/cmdline-tools" && cd "$SDK"
  curl -fL --progress-bar -o tools.zip "https://dl.google.com/android/repository/commandlinetools-$OS-11076708_latest.zip"
  unzip -q -o tools.zip -d cmdline-tools && rm -rf cmdline-tools/latest && mv cmdline-tools/cmdline-tools cmdline-tools/latest && rm tools.zip
  yes | "$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" --licenses >/dev/null || true
  "$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" "platform-tools" "emulator" "$IMG"
fi
export ANDROID_HOME="$SDK" ANDROID_SDK_ROOT="$SDK"
export PATH="$SDK/platform-tools:$SDK/emulator:$SDK/cmdline-tools/latest/bin:$PATH"

if ! "$SDK/emulator/emulator" -list-avds | grep -qx "$AVD"; then
  echo "Creating the phone '$AVD'..."
  [ -d "$SDK/system-images/android-34/google_apis_playstore/$ABI" ] || sdkmanager --sdk_root="$SDK" "$IMG"
  echo no | avdmanager create avd -n "$AVD" -k "$IMG" -d pixel_6 --force
fi

adb start-server >/dev/null
if adb devices | grep -q '^emulator-.*device$'; then
  pkill -CONT -f "qemu-system.*-avd $AVD" 2>/dev/null || true   # a frozen (sleeping) emulator hangs every adb call: wake it
  pkill -CONT -x netsimd 2>/dev/null || true
  echo "An emulator is already running: $(adb devices | grep '^emulator-' | head -1)"
else
  echo "Starting the phone (the first boot takes a minute or two)..."
  mkdir -p "$ROOT/data"
  nohup bash "$ROOT/scripts/emulator.sh" "$AVD" > "$ROOT/data/emulator.log" 2>&1 &
  disown
  adb wait-for-device
  until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; do sleep 3; done
fi
echo "Ready. The Mobile Agent in Docker finds it by itself within a few seconds. Stop it with: adb emu kill"
