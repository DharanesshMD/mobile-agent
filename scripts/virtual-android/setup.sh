#!/usr/bin/env bash
# One-time setup of the virtual Android host: a small Lima VM (Apple Virtualization.framework, Ubuntu 24.04 arm64) with Docker and the Android binder
# kernel module. Safe to run again. Free software only. Needs Homebrew.
set -euo pipefail
cd "$(dirname "$0")"
command -v limactl >/dev/null || brew install lima
limactl list -q | grep -qx rd || limactl create --name=rd --tty=false rd.yaml
limactl list --format '{{.Status}}' rd | grep -qx Running || limactl start rd --tty=false
limactl shell rd bash -s <<'EOS'
set -euo pipefail
sudo apt-get update -qq
# binder is a module in Ubuntu's generic kernel and ships in linux-modules-extra (reinstall this after a kernel upgrade)
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "linux-modules-extra-$(uname -r)"
echo binder_linux | sudo tee /etc/modules-load.d/redroid.conf >/dev/null
echo 'options binder_linux devices=binder,hwbinder,vndbinder' | sudo tee /etc/modprobe.d/redroid.conf >/dev/null
sudo modprobe binder_linux
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sudo sh
sudo docker pull redroid/redroid:13.0.0_64only-latest
EOS
echo "ready: ./redroid.sh start"
