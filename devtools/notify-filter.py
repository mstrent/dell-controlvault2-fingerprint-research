#!/usr/bin/env python3
"""Echo stdin and show desktop notifications for test prompts and progress,
so the person recording a umockdev test gets feedback away from the terminal.
Usage: <command> | notify-filter.py <user>"""
import subprocess
import sys

user = sys.argv[1]
uid = subprocess.check_output(['id', '-u', user], text=True).strip()
KEYS = ('>>>', 'enroll progress', 'cancelling', 'Traceback', 'Error', '###')

for line in sys.stdin:
    sys.stdout.write(line)
    sys.stdout.flush()
    if any(k in line for k in KEYS):
        subprocess.run(['runuser', '-u', user, '--', 'env',
                        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus' % uid,
                        'notify-send', '-t', '6000', '-a', 'cv2', 'Fingerprint test',
                        line.strip().lstrip('>').strip()[:200]],
                       check=False, stderr=subprocess.DEVNULL, timeout=5)
