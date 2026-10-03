#!/usr/bin/env python3
"""cvtool.py - talk directly to a Dell ControlVault2 (BCM5880, 0a5c:5834) to
probe the fingerprint-enrollment commit.

Framing (recovered from usbmon captures of the stock Broadcom TOD driver):
  host -> bulk OUT 0x01 : 0x2c-byte header + params
  chip -> intr IN  0x85 : u32 type (0 = reply ready, 3 = finger event), u32 len
  chip -> bulk IN  0x81 : reply (len bytes), status at +0x14
  header: u32 1 | u32 total | u16 cmd | u16 flags | u32 2 | u32 handle |
          u32 status | zeros | @0x28 u32 param_bytes
  param:  u32 kind | u32 len | data padded to 4

SAFETY: only command IDs the stock driver itself sends are allowed.

Usage (as root, with fprintd stopped; see run-cvtool.sh):
  cvtool.py commit-noenroll
  cvtool.py enroll [--commit "2:0 2:0 3:0"] [--commit "..."] ...
Each --commit is the p3..p5 spec "kind:len[:hex]"; they are tried in order on
the same enrollment until one returns status 0. Default: the stock layout.
"""
import argparse, os, struct, sys, time
import usb.core, usb.util

VID, PID = 0x0A5C, 0x5834
EP_OUT, EP_IN, EP_INT = 0x01, 0x81, 0x85
ALLOWED = {0x02: 'open', 0x04: 'close', 0x66: 'capture_start', 0x68: 'capture_cancel',
           0x6c: 'enroll_update', 0x6d: 'enroll_discard', 0x6e: 'enroll_commit',
           0x8a: 'enrollment_started', 0x2f: 'match', 0x0a: 'delete_template',
           0x39: 'get_version', 0x82: 'windows_enroll_prep'}
FLAGS_SYNC, FLAGS_ASYNC = 0x0440, 0x0442
CAPTURE_MODE_ENROLL = 0x23
LOG = None


def log(msg=''):
    print(msg)
    if LOG:
        LOG.write(msg + '\n')
        LOG.flush()


def hexdump(b, indent='      ', limit=256):
    if len(b) > limit:
        hexdump(b[:limit], indent, limit)
        log('%s... (%d more bytes)' % (indent, len(b) - limit))
        return
    for i in range(0, len(b), 16):
        c = b[i:i + 16]
        log('%s%04x  %-48s %s' % (indent, i, ' '.join('%02x' % x for x in c),
            ''.join(chr(x) if 32 <= x < 127 else '.' for x in c)))


def param(kind, data=b'', length=None):
    length = len(data) if length is None else length
    pad = (-len(data)) % 4
    return struct.pack('<II', kind, length) + data + b'\0' * pad


def u32(v):
    return struct.pack('<I', v)


def parse_spec(spec):
    """'2:0 2:0 3:4096' -> list of param bytes. 'kind:len[:hex]'; hex 'z' = len zero bytes."""
    out = []
    for item in spec.split():
        parts = item.split(':')
        kind, length = int(parts[0], 0), int(parts[1], 0)
        if len(parts) > 2 and parts[2] == 'z':
            data = b'\0' * length
        else:
            data = bytes.fromhex(parts[2]) if len(parts) > 2 else b''
        out.append(param(kind, data, length))
    return out


def parse_params(buf):
    out, off = [], 0x2c
    total = struct.unpack_from('<I', buf, 4)[0]
    while off + 8 <= min(total, len(buf)):
        kind, ln = struct.unpack_from('<II', buf, off)
        data = buf[off + 8:off + 8 + ln]
        out.append((kind, ln, data))
        off += 8 + ln + ((-ln) % 4)
    return out


class CV:
    def __init__(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            sys.exit('0a5c:5834 not found')
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        usb.util.claim_interface(self.dev, 0)
        self.handle = 0

    def release(self):
        usb.util.release_interface(self.dev, 0)
        usb.util.dispose_resources(self.dev)

    def read_int(self, timeout_ms):
        r = bytes(self.dev.read(EP_INT, 16, timeout=timeout_ms))
        return struct.unpack_from('<II', r) if len(r) >= 8 else (None, None)

    def call(self, cmd, params, hdr_handle=None, flags=FLAGS_SYNC, quiet=False):
        if cmd not in ALLOWED:
            raise RuntimeError('command 0x%02x is not on the allow-list' % cmd)
        body = b''.join(params)
        hh = self.handle if hdr_handle is None else hdr_handle
        hdr = struct.pack('<IIHHIII', 1, 0x2c + len(body), cmd, flags, 2, hh, 0)
        hdr += b'\0' * (0x28 - len(hdr)) + u32(len(body))
        msg = hdr + body
        log('>> 0x%02x %s (%d bytes)' % (cmd, ALLOWED[cmd], len(msg)))
        if not quiet:
            hexdump(msg)
        self.dev.write(EP_OUT, msg, timeout=3000)
        typ, ln = self.read_int(5000)
        while typ == 3:                      # stray finger event; keep waiting for the reply
            typ, ln = self.read_int(5000)
        reply = bytes(self.dev.read(EP_IN, max(ln, 64), timeout=3000))
        status = struct.unpack_from('<I', reply, 0x14)[0]
        log('<< 0x%02x status=0x%02x (%d bytes)' % (cmd, status, len(reply)))
        if not quiet or status:
            hexdump(reply)
        return status, reply

    # --- the operations, byte-for-byte as the stock driver sends them ---
    def open(self):
        st, r = self.call(0x02, [param(0, u32(0x44)), param(1, b'myAppID\0', 7),
                                 param(1, b'myUserID'), param(1, b'', 0)], hdr_handle=0)
        if st:
            sys.exit('open failed: 0x%x' % st)
        self.handle = struct.unpack_from('<I', r, 0x10)[0]
        log('   session handle = 0x%08x' % self.handle)

    def close(self):
        self.call(0x04, [param(0, u32(self.handle))], hdr_handle=0, quiet=True)

    def cancel(self):
        self.call(0x68, [param(0, u32(0))], hdr_handle=0, quiet=True)

    def discard(self):
        self.call(0x6d, [param(0, u32(0))], hdr_handle=0, quiet=True)

    def enrollment_started(self):
        self.call(0x8a, [param(0, u32(0))], hdr_handle=0, quiet=True)

    def capture_start(self, mode=CAPTURE_MODE_ENROLL):
        args = [param(0, u32(self.handle)), param(0, u32(2)), param(0, u32(mode))]
        st, r = self.call(0x66, args, flags=FLAGS_ASYNC, quiet=True)
        if st == 0x85:                        # capture already pending: cancel and retry, as the stock driver does
            self.cancel()
            st, r = self.call(0x66, args, flags=FLAGS_ASYNC, quiet=True)
        if st:
            raise RuntimeError('capture_start failed: 0x%x' % st)
        return parse_params(r)[0][2]          # 20-byte capture ID

    def wait_finger(self, timeout_s=60):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                typ, _ = self.read_int(1000)
            except usb.core.USBTimeoutError:
                continue
            if typ == 3:
                return True
        return False

    def update(self, capture_id):
        st, r = self.call(0x6c, [param(0, u32(self.handle)), param(0, capture_id),
                                 param(2, b'', 0)], quiet=True)
        if st:
            return st, None, None
        ps = parse_params(r)
        return st, ps[0][2][0], ps[1][2]     # done byte, 20-byte result ID

    def match(self, handles):
        lst = b''.join(u32(h) for h in handles)
        st, r = self.call(0x2f, [param(0, u32(self.handle)), param(0, u32(0x48)), param(0, u32(0x53e2)),
                                 param(0, u32(0)), param(2, lst)], flags=FLAGS_ASYNC)
        if st:
            return st, None, None
        ps = parse_params(r)
        return st, struct.unpack('<I', ps[0][2])[0], struct.unpack('<I', ps[1][2])[0]

    def delete_template(self, h):
        return self.call(0x0a, [param(0, u32(self.handle)), param(0, u32(h)), param(2, b'', 0)],
                         flags=FLAGS_ASYNC)[0]

    def commit(self, token, tail_params):
        return self.call(0x6e, [param(0, u32(self.handle)), param(0, token)] + tail_params)


def do_match(cv, handles, presses):
    cv.open()
    try:
        log('\n== match against %s ==' % ', '.join('0x%08x' % h for h in handles))
        for i in range(presses):
            cv.capture_start()
            print('   touch the sensor (press %d of %d)...' % (i + 1, presses), flush=True)
            if not cv.wait_finger():
                log('   no finger within 60 s'); break
            st, ok, which = cv.match(handles)
            if st:
                log('   press %d: status 0x%x' % (i + 1, st))
            else:
                log('   press %d: match=%d handle=0x%08x' % (i + 1, ok, which))
            cv.cancel()
    finally:
        cv.cancel()
        cv.close()


def do_stale_probe(cv, handles):
    """handles: valid,stale. Can a match without a capture test existence,
    and can one capture be matched again after a 0x1b?"""
    valid, stale = handles[0], handles[1]
    cv.open()
    try:
        log('\n== match with no capture pending (existence probe) ==')
        cv.cancel()
        for h in (valid, stale):
            st, ok, which = cv.match([h])
            log('   0x%08x -> status 0x%x%s' % (h, st, '' if st else ' match=%d' % ok))
        log('\n== one capture, mixed list, then the valid handle alone ==')
        cv.capture_start()
        print('   touch the sensor with the VALID finger...', flush=True)
        if not cv.wait_finger():
            log('   no finger within 60 s'); return
        for lst in ([valid, stale], [valid], [valid]):
            st, ok, which = cv.match(lst)
            log('   %s -> status 0x%x%s' % (', '.join('0x%08x' % h for h in lst), st,
                '' if st else ' match=%d handle=0x%08x' % (ok, which)))
    finally:
        cv.cancel()
        cv.close()


def do_capture_idle(cv, seconds):
    """Start a capture, touch nothing, and log every interrupt (and any data
    it announces) to see how the chip ends an unanswered capture."""
    cv.open()
    try:
        cv.capture_start()
        log('\n== capture started; logging interrupts for %d s (do not touch) ==' % seconds)
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                typ, ln = cv.read_int(1000)
            except usb.core.USBTimeoutError:
                continue
            log('   %6.2f s: interrupt type %s len %s' % (time.time() - t0, typ, ln))
            if ln:
                try:
                    data = bytes(cv.dev.read(EP_IN, max(ln, 64), timeout=2000))
                    log('   bulk IN %d bytes:' % len(data))
                    hexdump(data)
                except usb.core.USBTimeoutError:
                    log('   (no bulk data)')
            if typ == 3:                      # re-arm so the next touch is reported too
                cv.cancel()
                cv.capture_start()
                log('   re-armed')
    finally:
        cv.cancel()
        cv.close()


def do_capture_wait(cv, handle, seconds=60):
    """After a too-short finger event, does the same capture still deliver a
    later good press? Waits without re-arming, then matches on that capture."""
    cv.open()
    try:
        cv.capture_start()
        log('\n== capture started: quick tap, then a held press (no re-arm) ==')
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                typ, ln = cv.read_int(1000)
            except usb.core.USBTimeoutError:
                continue
            log('   %6.2f s: interrupt type %s len %s' % (time.time() - t0, typ, ln))
            if typ == 3 and ln == 0:
                st, ok, which = cv.match([handle])
                log('   match on this capture -> status 0x%x%s' % (st, '' if st else ' match=%d' % ok))
                break
    finally:
        cv.cancel()
        cv.close()


def do_reset_state(cv):
    """Discard any unfinished enrollment and cancel any pending capture
    (the stock driver's own abort sequence)."""
    cv.open()
    try:
        log('\n== discard enrollment, cancel capture ==')
        cv.call(0x68, [param(0, u32(0))], hdr_handle=0)
        cv.call(0x6d, [param(0, u32(0))], hdr_handle=0)
    finally:
        cv.close()


def do_send_82(cv):
    """Send 0x82 exactly as the Windows driver does at the start of an
    enrollment (plaintext, header handle 0, param 0:4 = 0; no session)."""
    log('\n== 0x82 (Windows enrollment prelude) ==')
    st, r = cv.call(0x82, [param(0, u32(0))], hdr_handle=0, flags=0x0040)
    log('   0x82 -> status 0x%x, %d bytes' % (st, len(r)))


def do_delete(cv, handles):
    cv.open()
    try:
        for h in handles:
            log('   delete 0x%08x -> status 0x%x' % (h, cv.delete_template(h)))
    finally:
        cv.close()


def do_commit_noenroll(cv):
    cv.open()
    log('\n== commit with NO enrollment in progress (zero token, stock tail) ==')
    cv.commit(b'\0' * 20, parse_spec('2:0 2:0 3:0'))
    log('\n== commit with NO enrollment in progress (random token, stock tail) ==')
    cv.commit(os.urandom(20), parse_spec('2:0 2:0 3:0'))
    cv.cancel()
    cv.close()


def do_enroll(cv, specs, max_presses):
    cv.open()
    committed = False
    try:
        committed = _enroll(cv, specs, max_presses)
    finally:
        cv.cancel()
        if not committed:
            cv.discard()
        cv.close()


def _enroll(cv, specs, max_presses):
    cv.enrollment_started()
    log('\n== enrollment: press and lift fully each time ==')
    good = bad = 0
    token = None
    for _ in range(max_presses):
        cap_id = cv.capture_start()
        print('   touch the sensor (%d accepted so far)...' % good, flush=True)
        if not cv.wait_finger():
            log('   no finger within 60 s, giving up')
            break
        st, done, rid = cv.update(cap_id)
        if st == 0x59:
            bad += 1
            log('   sample rejected (0x59) - lift and try again')
            continue
        if st:
            log('   unexpected update status 0x%x, stopping' % st)
            break
        good += 1
        log('   sample #%d accepted, done=%d' % (good, done))
        if done:
            token = rid
            break
    log('   accepted=%d rejected=%d' % (good, bad))
    if token is None:
        log('   enrollment did not complete; discarding')
    else:
        log('   result ID = %s' % token.hex())
        for spec in specs:
            log('\n== commit variant: p3..p5 = "%s" ==' % spec)
            try:
                st, r = cv.commit(token, parse_spec(spec))
            except usb.core.USBError as e:
                log('   USB error during commit: %s' % e)
                continue
            if st == 0:
                log('   *** COMMIT ACCEPTED with "%s" ***' % spec)
                for i, (k, ln, d) in enumerate(parse_params(r)):
                    log('   out param %d: kind=%d len=%d %s' % (i, k, ln, d.hex()))
                handle = struct.unpack('<I', parse_params(r)[-1][2])[0]
                log('   *** TEMPLATE HANDLE = %08x ***' % handle)
                return True
    return False


def do_version(cv, flags):
    try:
        st, r = cv.call(0x39, [param(0, u32(0))], hdr_handle=0, flags=flags, quiet=True)
    except usb.core.USBTimeoutError:
        log('   flags 0x%04x -> no reply (timeout)' % flags)
        return
    log('   flags 0x%04x -> status 0x%x' % (flags, st))
    if st == 0 and len(r) >= 0x34:
        kind, n = struct.unpack_from('<II', r, 0x2c)
        text = r[0x34:0x34 + n].split(b'\0')[0].decode('ascii', 'replace')
        log('   param kind %d, %d bytes:' % (kind, n))
        log(text)


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['commit-noenroll', 'enroll', 'match', 'delete', 'version', 'stale-probe', 'capture-idle', 'capture-wait', 'reset-state', 'send-82'])
    ap.add_argument('--commit', action='append', help='p3..p5 spec, e.g. "2:0 2:0 3:4096"')
    ap.add_argument('--max-presses', type=int, default=30)
    ap.add_argument('--handles', default='', help='comma-separated template handles (hex)')
    ap.add_argument('--presses', type=int, default=3)
    ap.add_argument('--log', default=None)
    ap.add_argument('--flags', default='0x0440', help='header flags for the version query')
    a = ap.parse_args()
    if a.log:
        LOG = open(a.log, 'a')
        log('#### %s %s' % (time.strftime('%F %T'), ' '.join(sys.argv[1:])))
    cv = CV()
    try:
        handles = [int(h, 16) for h in a.handles.split(',') if h]
        if a.action == 'version':
            do_version(cv, int(a.flags, 16))
        elif a.action == 'stale-probe':
            do_stale_probe(cv, handles)
        elif a.action == 'capture-idle':
            do_capture_idle(cv, a.presses)
        elif a.action == 'capture-wait':
            do_capture_wait(cv, handles[0])
        elif a.action == 'reset-state':
            do_reset_state(cv)
        elif a.action == 'send-82':
            do_send_82(cv)
        elif a.action == 'match':
            do_match(cv, handles, a.presses)
        elif a.action == 'delete':
            do_delete(cv, handles)
        elif a.action == 'commit-noenroll':
            do_commit_noenroll(cv)
        else:
            do_enroll(cv, a.commit or ['2:0 2:0 3:0'], a.max_presses)
    finally:
        cv.release()


if __name__ == '__main__':
    main()
