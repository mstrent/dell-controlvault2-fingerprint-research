#!/usr/bin/env bash
# Capture CV2 USB traffic (usbmon, text format) during one enrollment attempt.
# Run as: pkexec bash capture-enroll.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/enroll-usbmon.txt"
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }

modprobe usbmon 2>/dev/null || true   # built-in on Fedora kernels
echo ">>> capture script starting"
mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
BUS=""
for d in /sys/bus/usb/devices/*; do
  if [[ "$(cat "$d/idVendor" 2>/dev/null)" == 0a5c && "$(cat "$d/idProduct" 2>/dev/null)" == 5834 ]]; then
    BUS=$(cat "$d/busnum"); break
  fi
done
[[ -n "$BUS" ]] || { echo "0a5c:5834 not found" >&2; exit 1; }

cat "/sys/kernel/debug/usb/usbmon/${BUS}u" > "$OUT" &
CAP=$!
trap 'kill $CAP 2>/dev/null || true; chown "$USER_NAME" "$OUT"; echo "Saved $OUT"' EXIT
sleep 1
echo ">>> Enrolling for $USER_NAME - touch the sensor when asked (lift fully between presses)"
fprintd-enroll "$USER_NAME" || true
sleep 1
