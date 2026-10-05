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
  cvtool.py commit-fill --handles-file F [--max-commits N]
  cvtool.py delete (--handles H,H | --handles-file F) [--auth]
  cvtool.py enumerate [--type 7] [--buflen 1024]
  cvtool.py enumerate-direct [--type 7] [--buflen 1024]
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
           0x39: 'get_version', 0x82: 'windows_enroll_prep',
           # Read-only; ID and parameters from Broadcom's Apache-2.0 Citadel SDK
           # (sniten/citadel_sdk_2.1.1: CV_CMD_ENUMERATE_OBJECTS in cvinternal.h,
           # its dispatcher in cvmanager.c, cv_obj_type in cvapi.h)
           0x0d: 'enumerate_objects',
           # Read-only; CV_CMD_ENUMERATE_OBJECTS_DIRECT (cvmanager.c passes
           # a length/value pair, the object type and an in/out buffer)
           0x71: 'enumerate_objects_direct'}
CV_TYPE_FINGERPRINT = 7
FLAGS_SYNC, FLAGS_ASYNC = 0x0440, 0x0442
# The commit's attribute and authorization blocks and the delete's
# authorization, as the libfprint cv2 driver sends them
AUTH = '0101ff0000000d000c42726f6164636f6d57424600'
VENDOR_COMMIT = '2:8:0000040004000000 2:21:%s 3:0' % AUTH
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
    def open(self, options=0x44):
        # options: 0x04 CV_SYNCHRONOUS (+0x40, as the stock driver sends);
        # 0x08 is CV_SUPPRESS_UI_PROMPTS (Citadel SDK cvapi.h)
        st, r = self.call(0x02, [param(0, u32(options)), param(1, b'myAppID\0', 7),
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

    def delete_template(self, h, auth=False):
        block = param(2, bytes.fromhex(AUTH)) if auth else param(2, b'', 0)
        return self.call(0x0a, [param(0, u32(self.handle)), param(0, u32(h)), block],
                         flags=FLAGS_ASYNC, quiet=True)[0]

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


def do_match_each(cv, handles, fingers):
    """One capture per finger, matched against each handle on its own, to
    tell which stored templates belong to which finger."""
    cv.open()
    try:
        for finger in fingers:
            cv.capture_start()
            print('>>> touch the sensor: %s' % finger, flush=True)
            if not cv.wait_finger():
                log('   no finger within 60 s'); break
            res = []
            for h in handles:
                st, ok, _ = cv.match([h])
                res.append('%08x:%s' % (h, ('0x%x' % st) if st else ('MATCH' if ok else '-')))
            log('   %-14s %s' % (finger, ' '.join(res)))
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


def do_delete(cv, handles, auth=False):
    cv.open()
    try:
        counts, times = {}, {}
        for h in handles:
            t0 = time.monotonic()
            st = cv.delete_template(h, auth)
            dt = time.monotonic() - t0
            counts[st] = counts.get(st, 0) + 1
            times.setdefault(st, []).append(dt)
            log('   delete 0x%08x -> status 0x%x (%.3f s)' % (h, st, dt))
        log('   delete summary: %s' % ', '.join(
            'status 0x%x x%d (median %.3f s)' % (st, n, sorted(times[st])[len(times[st]) // 2])
            for st, n in sorted(counts.items())))
    finally:
        cv.close()


def read_handles_file(path):
    with open(path) as f:
        return [int(line.split()[0], 16) for line in f if line.strip()]


def do_commit_fill(cv, handles_file, max_commits, max_presses):
    """Enroll once (as the driver does), then commit the same enrollment
    result repeatedly until the chip refuses, a handle repeats, or
    max_commits. Every handle is appended to handles_file at once, for
    cleanup with: delete --handles-file F --auth. Then one press checks which
    handles still exist (a missing one fails a match with 0x1b)."""
    log('\n== 0x82 sensor reset ==')
    cv.call(0x82, [param(0, u32(0))], hdr_handle=0, flags=0x0040, quiet=True)
    cv.open()
    handles = []
    try:
        token = None
        good = 0
        log('\n== enrollment: press and lift fully each time ==')
        for _ in range(max_presses):
            cv.enrollment_started()            # before every capture, as the driver does
            cap_id = cv.capture_start()
            print('   touch the sensor (%d accepted so far)...' % good, flush=True)
            if not cv.wait_finger():
                log('   no finger within 60 s, giving up'); break
            st, done, rid = cv.update(cap_id)
            if st in (0x59, 0x89):
                log('   sample not usable (0x%x), press again' % st); continue
            if st:
                log('   unexpected update status 0x%x, stopping' % st); break
            good += 1
            log('   sample #%d accepted, done=%d' % (good, done))
            if done:
                token = rid; break
        if token is None:
            log('   enrollment did not complete'); cv.discard(); return
        log('   result ID = %s' % token.hex())

        log('\n== committing the same result up to %d times ==' % max_commits)
        t0 = time.time()
        stop = 'reached --max-commits'
        for i in range(max_commits):
            st, r = cv.call(0x6e, [param(0, u32(cv.handle)), param(0, token)] + parse_spec(VENDOR_COMMIT),
                            quiet=True)
            if st:
                stop = 'commit #%d failed with status 0x%x' % (i + 1, st)
                break
            h = struct.unpack('<I', parse_params(r)[-1][2])[0]
            if h in handles:
                stop = 'commit #%d returned handle 0x%08x again' % (i + 1, h); break
            handles.append(h)
            with open(handles_file, 'a') as f:
                f.write('%08x\n' % h); f.flush(); os.fsync(f.fileno())
            log('   commit #%d -> handle 0x%08x (%.1f s)' % (i + 1, h, time.time() - t0))
        log('   STOP: %s; %d new handles in %s' % (stop, len(handles), handles_file))

        if handles:
            log('\n== existence check: one press, then each handle matched alone ==')
            cv.cancel()
            cv.capture_start()
            print('   touch the sensor with the SAME finger...', flush=True)
            if not cv.wait_finger():
                log('   no finger within 60 s; skipping the check'); return
            present = missing = other = 0
            for h in handles:
                st, ok, which = cv.match([h])
                if st == 0:
                    present += 1
                elif st == 0x1b:
                    missing += 1; log('   0x%08x MISSING' % h)
                else:
                    other += 1; log('   0x%08x -> status 0x%x' % (h, st))
            log('   existence: %d present, %d missing, %d other' % (present, missing, other))
    finally:
        cv.cancel()
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


def do_enumerate(cv, probes, open_options=0x44, header_flags=FLAGS_SYNC):
    """List the handles of stored objects of one type (7 = fingerprint).
    Request: session, type, and an in/out buffer offer (kind 3, length only).
    probes: list of (type, buffer length), all sent in one session."""
    cv.open(open_options)
    try:
        for obj_type, buflen in probes:
            _enumerate(cv, obj_type, buflen, header_flags)
    finally:
        cv.close()


def _enumerate(cv, obj_type, buflen, header_flags=FLAGS_SYNC):
    log('\n== enumerate objects of type %d (buffer %d bytes, flags 0x%04x) ==' % (obj_type, buflen, header_flags))
    t0 = time.monotonic()
    st, r = cv.call(0x0d, [param(0, u32(cv.handle)), param(0, u32(obj_type)), param(3, b'', buflen)],
                    flags=header_flags, quiet=True)
    log('   reply after %.3f s' % (time.monotonic() - t0))
    params = parse_params(r) if len(r) > 0x2c else []
    for i, (k, ln, d) in enumerate(params):
        log('   out param %d: kind=%d len=%d' % (i, k, ln))
    if params:
        k, ln, d = params[0]
        handles = [struct.unpack_from('<I', d, o)[0] for o in range(0, len(d) - len(d) % 4, 4)]
        log('   status 0x%x, %d handle(s): %s' % (st, len(handles),
            ' '.join('%08x' % h for h in handles)))
    else:
        log('   status 0x%x, no output parameters' % st)


def do_enum_hashes(cv, obj_types, buflen):
    """0x71 called the way the SDK's cv_enumerate_objects_direct expects:
    arg 0 is the byte length of a list of 20-byte owner hashes, arg 1 is that
    list, arg 2 is the object type, arg 3 the buffer offer. One all-zero hash
    (length 20) makes the firmware's cvEnumObj set enumerateAll and list every
    object of the type in exactly one directory walk, so it terminates even
    when nothing matches (no runaway loop, no hang)."""
    SHA1_LEN = 20
    cv.open()
    try:
        for obj_type in obj_types:
            log('\n== enum (one zero hash) type %d, buffer %d ==' % (obj_type, buflen))
            t0 = time.monotonic()
            st, r = cv.call(0x71, [param(0, u32(SHA1_LEN)), param(0, b'\0' * SHA1_LEN),
                                   param(0, u32(obj_type)), param(3, b'', buflen)], quiet=True)
            dt = time.monotonic() - t0
            params = parse_params(r) if len(r) > 0x2c else []
            handles = []
            if params:
                k, ln, d = params[0]
                handles = [struct.unpack_from('<I', d, o)[0] for o in range(0, ln - ln % 4, 4)]
            log('   status 0x%x after %.3f s, %d handle(s): %s' % (st, dt, len(handles),
                ' '.join('%08x' % h for h in handles)))
    finally:
        cv.close()


def do_enumerate_direct(cv, obj_type, buflen, blobs=None):
    """0x71 exploration kept for the record. Its real arguments are a
    hash-list length, the hashes, the type and a buffer (see do_enum_hashes,
    which is the correct and safe way to call it). The variants here that put
    the session handle in the length field drive numHashs to ~16.7M and hang
    the chip when nothing fills the buffer; use enum-hashes instead."""
    cv.open()
    try:
        variants = [('session handle', u32(cv.handle), cv.handle),
                    ('session handle, header 0', u32(cv.handle), 0),
                    ('app ID', b'myAppID\0', cv.handle),
                    ('user ID', b'myUserID', cv.handle),
                    ('empty', b'', cv.handle),
                    ('empty, header 0', b'', 0)]
        if blobs and blobs[0].startswith('spec:'):
            # raw replacements for the first argument(s), "kind:len[:hex]" each
            variants = [(b[5:], b''.join(parse_spec(b[5:].replace('{h}', u32(cv.handle).hex()))), cv.handle)
                        for b in blobs]
        if blobs is not None and not blobs[0].startswith('spec:'):
            variants = [(v, b, h) for v, b, h in variants if v in blobs]
        for label, blob, hh in variants:
            log('\n== enumerate direct, type %d, buffer %d, blob: %s ==' % (obj_type, buflen, label))
            t0 = time.monotonic()
            first = blob if label.startswith(('0:', '1:', '2:', '3:')) else param(2, blob)
            st, r = cv.call(0x71, [first, param(0, u32(obj_type)), param(3, b'', buflen)],
                            hdr_handle=hh, quiet=True)
            log('   reply after %.3f s' % (time.monotonic() - t0))
            for i, (k, ln, d) in enumerate(parse_params(r) if len(r) > 0x2c else []):
                handles = [struct.unpack_from('<I', d, o)[0] for o in range(0, ln - ln % 4, 4)]
                log('   out param %d: kind=%d len=%d handles: %s' % (i, k, ln,
                    ' '.join('%08x' % h for h in handles)))
            log('   status 0x%x' % st)
    finally:
        cv.close()


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
    ap.add_argument('action', choices=['commit-noenroll', 'enroll', 'commit-fill', 'enumerate', 'enumerate-direct', 'enum-hashes', 'match', 'match-each', 'delete', 'version', 'stale-probe', 'capture-idle', 'capture-wait', 'reset-state', 'send-82'])
    ap.add_argument('--commit', action='append', help='p3..p5 spec, e.g. "2:0 2:0 3:4096"')
    ap.add_argument('--max-presses', type=int, default=30)
    ap.add_argument('--handles', default='', help='comma-separated template handles (hex)')
    ap.add_argument('--presses', type=int, default=3)
    ap.add_argument('--log', default=None)
    ap.add_argument('--handles-file', default=None, help='file of hex handles, one per line')
    ap.add_argument('--max-commits', type=int, default=300)
    ap.add_argument('--type', type=int, default=CV_TYPE_FINGERPRINT, help='object type to enumerate')
    ap.add_argument('--spec', action='append', help='enumerate-direct: first argument(s) as "kind:len[:hex]", {h} = session handle')
    ap.add_argument('--fingers', default='finger', help='match-each: comma-separated finger names, one press each')
    ap.add_argument('--types', default='', help='enumerate-direct: comma-separated types, session-handle blob only')
    ap.add_argument('--buflen', type=int, default=1024, help='bytes offered for the handle list')
    ap.add_argument('--probes', default='', help='enumerate: type:buflen,... (overrides --type/--buflen)')
    ap.add_argument('--open-options', default='0x44', help='enumerate: session options for 0x02 (0x08 = suppress UI prompts)')
    ap.add_argument('--header-flags', default='0x0440', help='enumerate: header flags (0x0200 = suppress UI prompts)')
    ap.add_argument('--auth', action='store_true', help='delete with the authorization block')
    ap.add_argument('--flags', default='0x0440', help='header flags for the version query')
    a = ap.parse_args()
    if a.log:
        LOG = open(a.log, 'a')
        # File names only: paths can contain the user name
        log('#### %s %s' % (time.strftime('%F %T'),
                            ' '.join(os.path.basename(x) for x in sys.argv[1:])))
    cv = CV()
    try:
        handles = [int(h, 16) for h in a.handles.split(',') if h]
        if a.handles_file and a.action == 'delete':
            handles += read_handles_file(a.handles_file)
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
        elif a.action == 'enum-hashes':
            do_enum_hashes(cv, [int(x, 0) for x in a.types.split(',')] if a.types else [a.type], a.buflen)
        elif a.action == 'match-each':
            do_match_each(cv, handles, a.fingers.split(','))
        elif a.action == 'match':
            do_match(cv, handles, a.presses)
        elif a.action == 'delete':
            do_delete(cv, handles, a.auth)
        elif a.action == 'enumerate':
            probes = ([tuple(int(x, 0) for x in p.split(':')) for p in a.probes.split(',')]
                      if a.probes else [(a.type, a.buflen)])
            do_enumerate(cv, probes, int(a.open_options, 0), int(a.header_flags, 0))
        elif a.action == 'enumerate-direct':
            for t in ([int(x, 0) for x in a.types.split(',')] if a.types else [a.type]):
                do_enumerate_direct(cv, t, a.buflen, ['spec:' + x for x in a.spec] if a.spec else
                                    ['session handle'] if a.types else None)
        elif a.action == 'commit-fill':
            if not a.handles_file:
                sys.exit('commit-fill needs --handles-file')
            do_commit_fill(cv, a.handles_file, a.max_commits, a.max_presses)
        elif a.action == 'commit-noenroll':
            do_commit_noenroll(cv)
        else:
            do_enroll(cv, a.commit or ['2:0 2:0 3:0'], a.max_presses)
    finally:
        cv.release()


if __name__ == '__main__':
    main()
