#!/bin/sh
# Container start: the app refuses a non-loopback HOST without APP_TOKEN, so make sure one exists.
# APP_TOKEN from the environment wins; otherwise a random one is created once and kept in the data volume.
set -eu
mkdir -p data/logs data/apk
# no /dev/kvm (Docker Desktop on Mac/Windows): the emulator can only run in software
[ -e /dev/kvm ] || export EMU_ACCEL=off
# an APK shipped with the image (docker/mobile-agent.apk) is offered on the pairing page
if [ -f /opt/docker/mobile-agent.apk ] && [ ! -f data/apk/mobile-agent.apk ]; then cp /opt/docker/mobile-agent.apk data/apk/mobile-agent.apk; fi
if [ -z "${APP_TOKEN:-}" ]; then
  f=data/.app_token
  if [ ! -s "$f" ]; then
    umask 077
    python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$f"
  fi
  APP_TOKEN="$(cat "$f")"
  export APP_TOKEN
  echo "No APP_TOKEN set: using the generated one in the data volume. Open http://localhost:<published port, 8000 by default>/#token=$APP_TOKEN"
fi
exec "$@"
