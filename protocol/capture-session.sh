#!/usr/bin/env bash
# Capture the CV2 plaintext protocol for verify/identify/delete, one pcap per
# phase, using the currently installed (patched TOD) driver via fprintd.
# Run as: pkexec bash capture-session.sh
#
# Phases (prints are restored at the end so fingerprint login keeps working):
#   01-list          list enrolled prints
#   02-verify-match  verify right-index with the RIGHT INDEX finger
#   03-verify-nomatch verify right-index with a DIFFERENT finger
#   04-enroll-left   enroll left-index
#   05-identify      verify "any" finger: left index, right index, then another finger
#   06-delete        delete all prints for the user
#   07-reenroll      re-enroll right-index (restores login)
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/captures"
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
mkdir -p "$OUT"

BUS=""
for d in /sys/bus/usb/devices/*; do
  if [[ "$(cat "$d/idVendor" 2>/dev/null)" == 0a5c && "$(cat "$d/idProduct" 2>/dev/null)" == 5834 ]]; then
    BUS=$(cat "$d/busnum"); break
  fi
done
[[ -n "$BUS" ]] || { echo "0a5c:5834 not found" >&2; exit 1; }

CAP=""
start_cap() { tcpdump -q -i "usbmon$BUS" -s 0 -U -w "$OUT/$1.pcap" 2>/dev/null & CAP=$!; sleep 1; }
stop_cap()  { sleep 1; kill "$CAP" 2>/dev/null; wait "$CAP" 2>/dev/null; CAP=""; }
cleanup()   { [[ -n "$CAP" ]] && stop_cap; chown -R "$USER_NAME" "$OUT"; echo ">>> captures in $OUT"; }
trap cleanup EXIT

phase() {   # phase <name> <instruction> <command...>
  local name=$1 msg=$2; shift 2
  echo; echo "=================== $name ==================="
  echo ">>> $msg"
  sleep 3
  start_cap "$name"
  "$@" || true
  stop_cap
}

phase 01-list           "No finger needed."                                  fprintd-list "$USER_NAME"
phase 02-verify-match   "Touch with your RIGHT INDEX finger."                fprintd-verify -f right-index-finger "$USER_NAME"
phase 03-verify-nomatch "Touch with a DIFFERENT finger (not right index)."   fprintd-verify -f right-index-finger "$USER_NAME"
phase 04-enroll-left    "Enroll your LEFT INDEX finger (press/lift until done)." fprintd-enroll -f left-index-finger "$USER_NAME"
for who in "LEFT INDEX" "RIGHT INDEX" "a finger that is NOT enrolled (e.g. thumb)"; do
  tag=$(echo "$who" | tr 'A-Z ' 'a-z-' | cut -c1-11)
  phase "05-identify-$tag" "Touch with $who." fprintd-verify "$USER_NAME"
done
phase 06-delete         "No finger needed. Deleting all prints for $USER_NAME." fprintd-delete "$USER_NAME"
phase 07-reenroll       "Re-enroll your RIGHT INDEX finger (restores login)."  fprintd-enroll -f right-index-finger "$USER_NAME"
