# CV2 libfprint driver: design

Date: 2026-10-02. Status: approved in conversation, pending written review.

Protocol reference: `protocol/PROTOCOL.md`. Project context: `HANDOFF.md`.

## Goal

An open-source libfprint **match-on-chip** driver for the Dell ControlVault2 /
Broadcom BCM5880 fingerprint reader (USB `0a5c:5834`), submitted upstream to
libfprint (freedesktop GitLab).

Success means: on a Latitude 7490 with firmware `00412015`, fprintd can enroll,
verify, identify and delete fingerprints with the open driver alone (no TOD,
no proprietary binary), and the driver passes libfprint's CI with its unit and
umockdev tests.

## Decisions

1. **Target:** upstream libfprint, following its conventions (GObject C,
   `FpiSsm`, umockdev replay tests). Protocol knowledge comes from USB captures
   only; nothing is taken from the proprietary binary.
2. **Structure:** a protocol layer (`cv2-proto`) and a device layer (`cv2`).
3. **Features (v1):** enroll, verify, identify, delete. No on-chip listing or
   clearing.
4. **Storage:** each print stores the chip-assigned 32-bit template handle as
   its driver data. fprintd's storage is the source of truth.
5. **Old firmware (< `00412015`):** the device probes and verify, identify and
   delete work. Enroll refuses with a "update ControlVault2 firmware using
   Dell's package" error. If the version can't be read, status `0x24` at commit
   maps to the same error.
6. **Scope:** fingerprint commands on interface 0 only. Never touch the
   smart-card interfaces or firmware update.
7. **Security model:** the standard plaintext match-on-chip posture, documented
   in `PROTOCOL.md` and in the driver's header comment.

## 1. Components and layout

Code lives in a libfprint fork at `~/projects/libfprint`, branch `cv2-driver`.

| Path | Responsibility |
|---|---|
| `libfprint/drivers/cv2/cv2-proto.h/.c` | Request builders, reply parser (header, parameter list, status), typed getters (session handle, capture ID, done flag, result ID, template handle, match result), per-command flags table, status-to-error table, version-string parser and firmware gate. Depends on GLib only. |
| `libfprint/drivers/cv2/cv2.h/.c` | `FpiDeviceCv2`: ID table `0a5c:5834`, features `VERIFY | IDENTIFY | STORAGE | STORAGE_DELETE`, scan type press, 4 enroll stages, the transport, one `FpiSsm` per operation, cleanup sub-SSM. |
| `libfprint/meson.build`, `meson_options.txt` | Driver registration. |
| `tests/test-cv2-proto.c` | Protocol unit tests. |
| `tests/cv2/`, `tests/cv2-cancel/` | umockdev recordings and scripts. |

Not claimed: `DUPLICATES_CHECK` (fprintd runs its own identify before enroll),
`STORAGE_LIST`, `STORAGE_CLEAR`.

## 2. Data flow

### Transport

`cv2_cmd (dev, request, callback)` runs a sub-SSM:

1. Bulk OUT `0x01`: the whole request.
2. Interrupt IN `0x85`: 8 bytes, `u32 type`, `u32 length`.
   - `type 0`: continue.
   - `type 3` (finger event): stray (for example, queued after a cancel);
     log at debug level and read the interrupt again.
   - anything else: `FP_DEVICE_ERROR_PROTO`.
3. Bulk IN `0x81`: `length` bytes.
4. Parse into a `Cv2Reply`. The callback gets the reply or an error.

Command flags come from a per-command table: `0x0442` for `0x66`, `0x2f` and
`0x0a`; `0x0440` for everything else.

`cv2_wait_finger (dev, callback)` reads interrupt IN with no timeout, using the
operation's cancellable, until it gets `type 3`. It then calls
`fpi_device_report_finger_status (FP_FINGER_STATUS_PRESENT)`.

One command is outstanding at a time.

### Device open / close

- Open: claim interface 0, then send `0x39` (get version) in plaintext form
  (flags `0x0440`). On success, store `USH_REL_UPGRADE_VER`. On any failure,
  record the version as unknown; open still succeeds.
- Close: release interface 0.
- Each operation opens its own chip session (`0x02`) and closes it (`0x04`),
  as the stock driver does.

### Enroll

1. If the version is known and older than `00412015`, fail immediately with
   the firmware error (`FP_DEVICE_ERROR_NOT_SUPPORTED`, custom message).
2. `0x02` open session, then `0x8a` begin enrollment (sets `enrolling`).
3. Loop:
   - `0x66` capture start, mode `0x23` → capture ID. On `0x85`: `0x68`
     cancel, retry once.
   - Wait for finger.
   - `0x6c` sample with the capture ID:
     - status `0x59`: `fpi_device_enroll_progress` with
       `FP_DEVICE_RETRY_GENERAL`; loop.
     - status 0: keep the returned ID; report progress (capped at stage 3
       until done); if the done flag is 1, leave the loop.
4. `0x6e` commit with the last ID → template handle. Status `0x24` maps to the
   firmware error.
5. `0x04` close session.
6. Set the print's driver data to `GVariant "(u)"` (the template handle), and
   complete the enroll.

Completion is driven by the done flag, not by counting samples.

### Verify and identify

One SSM serves both; verify is identify with one handle.

1. Collect the template handles from the print (verify) or gallery
   (identify). Prints without valid `"(u)"` driver data give
   `FP_DEVICE_ERROR_DATA_INVALID`. An empty gallery reports no match without
   capturing.
2. `0x02` open session.
3. `0x66` capture start, mode `0x23` (`0x85` handled as in enroll).
4. Wait for finger.
5. `0x2f` match with the handle list (constants `0x48`, `0x53e2`, `0`).
   - `1` + a handle we sent: match, with the corresponding print.
   - `1` + a handle we didn't send: `FP_DEVICE_ERROR_PROTO`.
   - `0`: no match.
6. `0x68` cancel capture, `0x04` close session.

The stock driver's `0x66` mode `0x48` call after the match (always status
`0x47`) is skipped; tests showed no effect from skipping it. The `0x68` cancel
is kept so the chip ends with no capture pending.

### Delete

`0x02` open session, `0x0a` delete with the template handle (~1.4 s), `0x04`
close session.

## 3. Error handling and cancellation

### Status mapping

One table in `cv2-proto.c`, keyed by command and status:

| Where | Status | Result |
|---|---|---|
| `0x6c` | `0x59` | retry, `FP_DEVICE_RETRY_GENERAL` |
| `0x66` | `0x85` | `0x68` cancel, retry once; second `0x85` → `FP_DEVICE_ERROR_BUSY` |
| `0x2f` | `0x47` | no match (only seen with an empty list, which is not sent) |
| `0x6e` | `0x24` | `FP_DEVICE_ERROR_NOT_SUPPORTED`, firmware-update message |
| `0x0a` | nonzero | `FP_DEVICE_ERROR_GENERAL` with the status in the message (refine once stale-handle behaviour is known) |
| any | other nonzero | `FP_DEVICE_ERROR_PROTO` with command and status in the message |

### Reply validation

`FP_DEVICE_ERROR_PROTO` for: reply command ID ≠ request command ID, header
length ≠ bytes read, parameter area overrunning the message, missing or
wrong-kind parameters for the command.

### USB errors

- `G_USB_DEVICE_ERROR_NO_DEVICE` → `FP_DEVICE_ERROR_REMOVED`.
- Other USB errors are passed through.
- Command transfers: 5 s timeout. Finger wait: no timeout; it ends on a
  finger event or cancellation.

### Cleanup

Each operation tracks `session_open`, `capture_pending` and `enrolling`. When
an operation fails or is cancelled, its completion handler runs a cleanup
sub-SSM, then reports the original error. Cleanup uses a fresh,
non-cancelled cancellable and sends, in order:

1. `0x68` cancel capture, if `capture_pending`;
2. `0x6d` discard enrollment, if `enrolling`;
3. `0x04` close session, if `session_open`.

Cleanup errors are logged and ignored; they never replace the original error.

### Cancellation

All transfers, including the finger wait, use `fpi_device_get_cancellable ()`.
Cancellation surfaces as `G_IO_ERROR_CANCELLED` and goes through cleanup, so
the chip is idle for the next operation. No custom `cancel` vfunc.

### Stale chip state

Handled reactively: `0x85` on capture start triggers cancel and retry. Nothing
is sent at device open, because what `0x6d` does with no pending enrollment
is unknown. If a leftover enrollment is found to break `0x8a`, add a
best-effort `0x68` + `0x6d` at open, backed by a test.

### Suspend

No suspend vfunc in v1; libfprint's default handling applies, and cleanup
leaves the chip in a known state.

## 4. Testing

### Protocol unit tests (`tests/test-cv2-proto.c`)

Fixtures are bytes from `cvtool.log` and `protocol/captures/*.pcap`.

- Builders: `0x02`, `0x66`, `0x6c`, `0x6e`, `0x2f` (1 and 3 handles), `0x0a`,
  `0x68`, `0x6d`, `0x04` are byte-identical to captures, flags included.
- Parser: good replies for each command and typed getters; header-only failure
  replies.
- Malformed replies: truncated header, length mismatch, wrong command ID,
  parameter overrun, wrong parameter kind → protocol error.
- Status table: every row of the mapping above.
- Version parser: the real version string, missing keys, reordered keys,
  garbage. Firmware gate: `00412001` refuses enroll, `00412015` allows it,
  unknown allows it.

### umockdev replay

Recorded on the Latitude 7490 with the finished driver.

- `tests/cv2/` (`device`, `custom.pcapng`, `custom.py`), one session: open;
  enroll right index with at least one deliberate `0x59` rejection; verify
  right index (match); verify a different finger (no match); enroll left
  index; identify against both (match, then no match); delete both; close.
- `tests/cv2-cancel/`: start enroll, cancel during the finger wait after one
  accepted sample, check the cleanup traffic (`0x68`, `0x6d`, `0x04`), then a
  verify to show the chip is usable.

Both are registered in `tests/meson.build`. Recordings contain only session
handles, capture IDs and template handles, no biometric data. Check the USB
descriptors for a device serial number before committing them.

### Manual hardware checklist (before submitting upstream)

First remove the patched TOD driver and the fprintd debug drop-in.

- fprintd enroll, verify, login via `sudo` and GDM.
- Prints survive a reboot.
- Delete.
- Ctrl-C during `fprintd-enroll` and `fprintd-verify`; the next operation
  works.
- Suspend during a verify.

### Not testable on this machine

Old-firmware behaviour (version gate, `0x24` mapping) is covered by unit tests
only. The upstream merge request asks owners of old firmware to test. Device
removal (internal reader) is covered by unit tests only.

## Implementation prerequisites and risks

1. **First step:** confirm the plaintext `0x39` request with `cvtool.py`
   (add `0x39` to its allow-list). Device open depends on it.
2. **Finger lift between enroll samples:** unknown whether the chip requires
   it. Check the raw interrupt traffic in
   `protocol/captures/04-enroll-left.pcap` (`cvdump.py` does not decode
   endpoint `0x85`). If the chip needs a lift, add a finger-removed wait and
   `FP_FINGER_STATUS_NONE` reporting between samples.
3. **Stale handles:** behaviour of `0x2f` / `0x0a` with a deleted handle is
   unknown (`PROTOCOL.md` open question 3); the error mapping is provisional.
4. **Build dependencies** are not installed yet (meson, gcc, glib2-devel,
   libgusb-devel, gobject-introspection-devel, umockdev-devel, …).

## Amendments (2026-10-02, from implementation planning)

Captures examined during planning refine the design above; where they
conflict, this section wins.

1. Bulk IN replies are read with a 4196-byte buffer. The header's total length
   must be ≤ bytes read; trailing bytes are ignored (the commit reply's
   interrupt announced 65 bytes for a 64-byte message).
2. The version text is newline-separated `KEY:VALUE` lines.
3. There is no finger-lift wait between enroll samples; the stock driver
   doesn't wait and the chip raises one finger event per press.
4. Command transfers are not cancelled mid-flight. The transport checks for
   cancellation before sending each command; only the finger wait uses the
   cancellable. This prevents an unread reply from desynchronising the next
   command.
5. Cleanup always sends `0x68` when a session is open (it returns 0 with
   nothing pending), so there is no `capture_pending` flag.
6. A reply whose command ID doesn't match the request is discarded (at most
   twice) before failing with a protocol error.
7. Finger status: `NEEDED` while waiting for a press, `PRESENT` on the finger
   event, `NONE` after the reply and at operation end.
8. A failing `0x04` after a successful operation is logged, not reported.
9. Device removal: libfprint's core already reports `FP_DEVICE_ERROR_REMOVED`
   for operations that complete after removal, so the driver has no mapping
   for `G_USB_DEVICE_ERROR_NO_DEVICE` and no unit test for it.
10. The plaintext version query (`0x39`, flags `0x0440`) works on the
    reader (tested 2026-10-02), so the firmware gate uses it at open.
