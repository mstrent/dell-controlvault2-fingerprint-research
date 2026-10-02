#!/usr/bin/env bash
# Full-payload usbmon capture (pcap) of one enrollment attempt.
# Run as: pkexec bash capture-full.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/enroll-full.pcap"
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }

BUS=""
for d in /sys/bus/usb/devices/*; do
  if [[ "$(cat "$d/idVendor" 2>/dev/null)" == 0a5c && "$(cat "$d/idProduct" 2>/dev/null)" == 5834 ]]; then
    BUS=$(cat "$d/busnum"); break
  fi
done
[[ -n "$BUS" ]] || { echo "0a5c:5834 not found" >&2; exit 1; }

echo ">>> capturing usbmon$BUS (full payloads)"
tcpdump -q -i "usbmon$BUS" -s 0 -U -w "$OUT" 2>/dev/null &
CAP=$!
trap 'kill $CAP 2>/dev/null || true; wait $CAP 2>/dev/null || true; chown "$USER_NAME" "$OUT"; echo "Saved $OUT"' EXIT
sleep 1
echo ">>> Enrolling for $USER_NAME - press, lift fully, repeat"
fprintd-enroll "$USER_NAME" || true
sleep 1
