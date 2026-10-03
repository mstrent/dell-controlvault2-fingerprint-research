#!/usr/bin/env bash
# Record the libfprint cv2 umockdev tests (tests/cv2 and tests/cv2-cancel)
# with fprintd out of the way, with desktop notifications for the prompts.
# Run as: pkexec bash devtools/record-tests.sh [cv2|cancel|both]
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
SRC="$(getent passwd "$USER_NAME" | cut -d: -f6)/projects/libfprint"
WHICH="${1:-both}"

restore() {
  systemctl unmask --runtime fprintd >/dev/null 2>&1 || true
  systemctl start fprintd || true
  chown -R "$USER_NAME" "$SRC/tests/cv2" "$SRC/tests/cv2-cancel" 2>/dev/null || true
  echo ">>> fprintd restored"
}
trap restore EXIT
modprobe usbmon
systemctl mask --runtime fprintd >/dev/null
systemctl stop fprintd
export PYTHONUNBUFFERED=1
rc=0
if [[ $WHICH == cv2 || $WHICH == both ]]; then
  python3 "$SRC/build/tests/create-driver-test.py" --test custom cv2 2>&1 \
    | python3 "$HERE/notify-filter.py" "$USER_NAME"; rc=$((rc | PIPESTATUS[0]))
fi
if [[ $WHICH == cancel || $WHICH == both ]]; then
  python3 "$SRC/build/tests/create-driver-test.py" --test custom cv2 cancel 2>&1 \
    | python3 "$HERE/notify-filter.py" "$USER_NAME"; rc=$((rc | PIPESTATUS[0]))
fi
exit $rc
