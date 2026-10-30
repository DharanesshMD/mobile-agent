#!/usr/bin/env bash
# Install an APK on the games phone (Redroid, adb at localhost:5565): install-apk.sh <file.apk> [more.apk ...]
# The games phone has no Google Play, so apps come from APK files. Get free games from F-Droid (https://f-droid.org/) and check the sha256
# against the value on the app's F-Droid page before installing; see docs/virtual-android.md.
set -euo pipefail
export PATH="/opt/homebrew/bin:${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}/platform-tools:$PATH"
SERIAL=localhost:5565
[ $# -ge 1 ] || { echo "usage: $0 <file.apk> [more.apk ...]"; exit 2; }
adb connect "$SERIAL" >/dev/null
for apk in "$@"; do
  [ -f "$apk" ] || { echo "no such file: $apk"; exit 1; }
  echo "sha256 $(shasum -a 256 "$apk" | cut -d' ' -f1)  $(basename "$apk")"
  adb -s "$SERIAL" install -r "$apk"
done
