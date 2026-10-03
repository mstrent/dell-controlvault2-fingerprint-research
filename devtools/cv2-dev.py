#!/usr/bin/env python3
"""Drive the cv2 libfprint driver from a build tree, for hardware checks.

Run through run-dev.sh (root, fprintd stopped):
  pkexec bash devtools/run-dev.sh info
  pkexec bash devtools/run-dev.sh enroll right-index
  pkexec bash devtools/run-dev.sh verify right-index
  pkexec bash devtools/run-dev.sh identify
  pkexec bash devtools/run-dev.sh delete right-index
  pkexec bash devtools/run-dev.sh verify-bogus
  pkexec bash devtools/run-dev.sh cancel-enroll
Enrolled prints are saved in devtools/prints/<finger>.print.
"""
import os
import sys

BUILD = os.environ.get('LIBFPRINT_BUILD',
                       os.path.expanduser('~/projects/libfprint/build'))
LIB = os.path.join(BUILD, 'libfprint')

# Re-exec with the build tree's library and typelib, like
# tests/create-driver-test.py does.
if os.environ.get('LD_LIBRARY_PATH') != LIB:
    os.environ['LD_LIBRARY_PATH'] = LIB
    os.environ['GI_TYPELIB_PATH'] = LIB
    os.environ['FP_DRIVERS_ALLOWLIST'] = 'cv2'
    os.environ.setdefault('G_MESSAGES_DEBUG', 'libfprint-cv2 libfprint-device')
    os.execv(sys.executable, [sys.executable] + sys.argv)

import gi
gi.require_version('FPrint', '2.0')
from gi.repository import FPrint, GLib, Gio

PRINTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prints')
FINGERS = {
    'right-index': FPrint.Finger.RIGHT_INDEX,
    'left-index': FPrint.Finger.LEFT_INDEX,
    'right-thumb': FPrint.Finger.RIGHT_THUMB,
}


def open_device():
    ctx = FPrint.Context()
    ctx.enumerate()
    devs = [d for d in ctx.get_devices() if d.get_driver() == 'cv2']
    if not devs:
        sys.exit('no cv2 device (is the driver built? is fprintd stopped?)')
    d = devs[0]
    d.open_sync()
    return ctx, d


def print_path(finger):
    return os.path.join(PRINTS, finger + '.print')


def save(finger, p):
    os.makedirs(PRINTS, exist_ok=True)
    with open(print_path(finger), 'wb') as f:
        f.write(p.serialize())


def load(finger):
    with open(print_path(finger), 'rb') as f:
        return FPrint.Print.deserialize(f.read())


def progress(dev, stage, pnt, data, error):
    note = ' (retry: %s)' % error.message if error else ''
    print('  stage %d/%d%s' % (stage, dev.get_nr_enroll_stages(), note))


def do_enroll(d, finger):
    print('Press %s on the reader, lifting between presses.' % finger)
    template = FPrint.Print.new(d)
    template.set_finger(FINGERS[finger])
    p = d.enroll_sync(template, None, progress, None)
    save(finger, p)
    print('ENROLLED %s' % finger)


def do_cancel_enroll(d, ctx):
    cancellable = Gio.Cancellable()
    stages = []
    result = {}

    def on_progress(dev, stage, pnt, data, error):
        progress(dev, stage, pnt, data, error)
        if not error:
            stages.append(stage)

    def on_status(dev, pspec):
        if (stages and not cancellable.is_cancelled() and
                dev.get_finger_status() & FPrint.FingerStatusFlags.NEEDED):
            print('  cancelling while waiting for the second press')
            cancellable.cancel()

    def done(dev, res):
        try:
            dev.enroll_finish(res)
            result['error'] = None
        except GLib.Error as e:
            result['error'] = e

    handler = d.connect('notify::finger-status', on_status)
    print('Press right-index ONCE, then wait.')
    template = FPrint.Print.new(d)
    template.set_finger(FPrint.Finger.RIGHT_INDEX)
    d.enroll(template, cancellable=cancellable, progress_cb=on_progress,
             callback=done)
    while 'error' not in result:
        ctx.iteration(True)
    d.disconnect(handler)
    e = result['error']
    ok = e is not None and e.matches(Gio.io_error_quark(),
                                     Gio.IOErrorEnum.CANCELLED)
    print('CANCELLED' if ok else 'UNEXPECTED RESULT: %r' % e)
    print('finger status after cancel: %s' % d.get_finger_status())


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    action = sys.argv[1]
    finger = sys.argv[2] if len(sys.argv) > 2 else None
    ctx, d = open_device()
    try:
        if action == 'info':
            print('driver %s, %s' % (d.get_driver(), d.get_name()))
            print('features: %s' % d.get_features())
            print('enroll stages: %d' % d.get_nr_enroll_stages())
        elif action == 'enroll':
            do_enroll(d, finger)
        elif action == 'verify':
            print('Press a finger to check against %s.' % finger)
            ok, _ = d.verify_sync(load(finger))
            print('MATCH' if ok else 'NO MATCH')
        elif action == 'identify':
            names = sorted(f[:-6] for f in os.listdir(PRINTS) if f.endswith('.print'))
            gallery = [load(n) for n in names]
            print('Press a finger; gallery: %s' % ', '.join(names))
            match, _ = d.identify_sync(gallery)
            if match is None:
                print('NO MATCH')
            else:
                print('MATCH %s' % names[[g.equal(match) for g in gallery].index(True)])
        elif action == 'delete':
            d.delete_print_sync(load(finger))
            os.remove(print_path(finger))
            print('DELETED %s' % finger)
        elif action == 'verify-bogus':
            try:
                d.verify_sync(FPrint.Print.new(d))
                print('UNEXPECTED: verify succeeded')
            except GLib.Error as e:
                invalid = e.matches(FPrint.DeviceError.quark(),
                                    FPrint.DeviceError.DATA_INVALID)
                print('DATA_INVALID' if invalid else 'UNEXPECTED ERROR: %s' % e)
        elif action == 'cancel-enroll':
            do_cancel_enroll(d, ctx)
        else:
            sys.exit('unknown action %s' % action)
    finally:
        d.close_sync()


if __name__ == '__main__':
    main()
