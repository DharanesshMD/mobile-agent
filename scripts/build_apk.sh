#!/usr/bin/env bash
# Build the Mobile Agent app (debug APK) and put it where the phone gateway serves it: data/apk/mobile-agent.apk (git-ignored), with its SHA-256.
# Needs JDK 17 and the Android SDK (default: /opt/homebrew/share/android-commandlinetools; override with ANDROID_HOME).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
cd "$ROOT/android"
[ -f local.properties ] || echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew assembleDebug --no-daemon -q
mkdir -p "$ROOT/data/apk"
cp app/build/outputs/apk/debug/app-debug.apk "$ROOT/data/apk/mobile-agent.apk"
echo "Built $ROOT/data/apk/mobile-agent.apk ($(du -h "$ROOT/data/apk/mobile-agent.apk" | cut -f1))"
shasum -a 256 "$ROOT/data/apk/mobile-agent.apk"
