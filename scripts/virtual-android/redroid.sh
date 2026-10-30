#!/usr/bin/env bash
# Control the Redroid (Android 13 arm64, in a container) phone: start | stop | shutdown | pause | resume | state | status | adb
#   stop      stops the Android container (the Lima VM stays up)
#   shutdown  stops the container AND the Lima VM (frees about 1-2 GB of the Mac's RAM; the next start takes about 15 s instead of 5 s)
#   state     prints one word for the agent: stopped | paused | running
# The agent only needs an adb endpoint: localhost:5565.
set -euo pipefail
NAME=rd; VM=rd; PORT=5565; IMAGE=redroid/redroid:13.0.0_64only-latest
WIDTH=${WIDTH:-720}; HEIGHT=${HEIGHT:-1280}; DPI=${DPI:-320}
in_vm() { limactl shell "$VM" bash -c "$*"; }
docker_() { in_vm "sudo docker $*"; }
vm_running() { limactl list --format '{{.Status}}' "$VM" 2>/dev/null | grep -qx Running; }
vm_up() { limactl list --format '{{.Status}}' "$VM" 2>/dev/null | grep -qx Running || limactl start "$VM" --tty=false; }
booted() { [ "$(docker_ exec $NAME getprop sys.boot_completed 2>/dev/null || true)" = "1" ]; }
case "${1:-status}" in
  start)
    vm_up
    in_vm "lsmod | grep -q binder_linux || sudo modprobe binder_linux devices=binder,hwbinder,vndbinder"
    if docker_ ps -aq -f name=^$NAME\$ | grep -q .; then docker_ start $NAME >/dev/null
    else
      in_vm "mkdir -p ~/rd-data && sudo docker run -itd --privileged --name $NAME -v ~/rd-data:/data -p $PORT:5555 $IMAGE \
        androidboot.redroid_width=$WIDTH androidboot.redroid_height=$HEIGHT androidboot.redroid_dpi=$DPI androidboot.use_memfd=1 >/dev/null"
    fi
    t0=$(date +%s); until booted; do sleep 1; [ $(( $(date +%s) - t0 )) -lt 180 ] || { echo "Android did not boot in 180 s"; exit 1; }; done
    echo "Android booted in $(( $(date +%s) - t0 )) s"
    adb connect localhost:$PORT ;;
  stop)    adb disconnect localhost:$PORT >/dev/null 2>&1 || true; docker_ stop -t 5 $NAME >/dev/null; echo stopped ;;
  shutdown) vm_running && { docker_ stop -t 5 $NAME >/dev/null 2>&1 || true; }
            adb disconnect localhost:$PORT >/dev/null 2>&1 || true; limactl stop "$VM" >/dev/null 2>&1 || true; echo shutdown ;;
  state)   vm_running || { echo stopped; exit 0; }
           case "$(docker_ inspect -f '{{.State.Status}}' $NAME 2>/dev/null || true)" in running) echo running ;; paused) echo paused ;; *) echo stopped ;; esac ;;
  pause)   docker_ pause $NAME >/dev/null && echo paused ;;     # freezes every process, about 0.2 s; resume is as fast and adb carries on
  resume)  docker_ unpause $NAME >/dev/null && echo resumed ;;
  status)  limactl list "$VM"; docker_ ps -a --format '"{{.Names}} {{.Status}}"' ;;
  adb)     shift; adb -s localhost:$PORT "$@" ;;
  *) echo "usage: $0 start|stop|shutdown|pause|resume|state|status|adb ..."; exit 2 ;;
esac
