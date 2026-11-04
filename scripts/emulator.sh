#!/bin/bash
# Start the Play Store emulator with the light profile: Mac GPU rendering, capped cores and memory, no window, clean boot.
# Measured on an M-series Mac: about half the CPU of software rendering (83% avg / 246% peak vs 160-180% / ~490%).
set -e
export ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$HOME/.android/avd}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
AVD="${1:-agent34ps}"
exec emulator -avd "$AVD" -no-window -no-audio -no-boot-anim -no-metrics -no-snapshot \
  ${EMU_ACCEL:+-accel "$EMU_ACCEL"} -gpu "${EMU_GPU:-host}" -cores "${EMU_CORES:-3}" -memory "${EMU_MEMORY:-2048}"
