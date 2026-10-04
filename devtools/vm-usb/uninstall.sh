#!/usr/bin/env bash
# Remove the udev hook installed by install.sh (the VM's startupPolicy stays).
# Run as: pkexec bash devtools/vm-usb/uninstall.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
rm -f /etc/udev/rules.d/90-cv2-vm-usb.rules /usr/local/sbin/cv2-vm-usb
udevadm control --reload
echo ">>> removed the cv2 VM USB hook"
