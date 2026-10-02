#!/usr/bin/env bash
# Run cvtool.py with fprintd out of the way, then restore fprintd.
# Run as: pkexec bash run-cvtool.sh <cvtool args...>
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
LOGF="$HERE/cvtool.log"

rpm -q python3-pyusb >/dev/null 2>&1 || dnf -y install python3-pyusb

restore() {
  systemctl unmask --runtime fprintd >/dev/null 2>&1 || true
  systemctl start fprintd || true
  [[ -f "$LOGF" ]] && chown "$USER_NAME" "$LOGF"
  echo ">>> fprintd restored; log: $LOGF"
}
trap restore EXIT
systemctl mask --runtime fprintd >/dev/null
systemctl stop fprintd
python3 -u "$HERE/cvtool.py" --log "$LOGF" "$@"
