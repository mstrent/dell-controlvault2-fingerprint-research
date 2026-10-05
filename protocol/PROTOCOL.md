# Dell ControlVault 2 (Broadcom BCM5880, USB `0a5c:5834`) fingerprint protocol

Status: **draft**. Covers the plaintext host interface the fingerprint function uses.

## Provenance

Everything here comes from **USB traffic captures** (Linux `usbmon`) of the
reader on a Dell Latitude 7490 owned by the author, while it was driven by
fprintd. Command names describe the **observed behavior**; they are not taken
from vendor headers or binaries. Facts not yet observed are listed under
[Open questions](#open-questions) rather than guessed.

Out of scope: the reader's smart-card (CCID) interfaces, firmware update, and
the protected session mode used by the Windows driver. An open driver should
use only the fingerprint commands documented here.

2026-10-03: The commit and delete blocks were captured from Dell's Linux driver as patched by [grosa787/dell-controlvault2-fingerprint-linux#13](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/pull/13) to send the values Dell's Windows driver uses (Windows sends them inside its protected session). (`captures/08-tod-pr13-commit-delete.pcap`).

Evidence files (in `../`): `enroll-full.pcap`, `enroll-usbmon*.txt`,
`cvtool.log`, and `captures/*.pcap` from `capture-session.sh`.

## Device

| | |
|---|---|
| USB ID | `0a5c:5834` "Broadcom Corp / 5880", USB 1.1 |
| Interfaces | 0: class `0xFE` (fingerprint/CV channel), 1–2: CCID smart card, 3: vendor `0xFF` |
| Interface 0 endpoints | bulk OUT `0x01`, bulk IN `0x81` (64-byte packets), interrupt IN `0x85` (16 bytes) |
| Kernel driver | none needed; interface 0 is claimed from userspace |

### Firmware requirement

The firmware version string is returned by command `0x39` as newline-separated
`KEY:VALUE` lines in a kind-1 string parameter (796 bytes on `00412015`; the
reply is 848 bytes, several USB packets). Relevant keys:

| Key | Observed values |
|---|---|
| `USH_CHIPID` | `05810211` |
| `USH_REL_UPGRADE_VER` | `00047026` (4.7.26, 2017; factory on a Latitude 7480, reported by a tester) and `00412001` (factory on this 7490): enroll commit fails with `0x24` · `00412015`: enroll works |

`00412015` (4.12.015) ships only in Dell's *ControlVault2 Driver and
Firmware* package 4.12.11.15 (A25, driver ID NX3HH). The package Dell lists as
newest for the 7480, 43NG7 (4.12.5.8, A21), contains 4.12.001. Between the
two packages only `bcmLynx_1.otp` and `bcmLynx_7.otp` differ. While
flashing, the chip re-enumerates twice as `0a5c:5831`, so a VM should get the
reader by USB port, not by vendor/product ID (reported on the 7480).

The `0x24` failures were with empty commit blocks. With the vendor's
attribute and authorization blocks (see [Commands](#commands)), `00412001`
enrolls, matches, rejects a wrong finger and deletes (2026-10-04, a tester's
Precision 3520, three runs). `00047026` is untested with the blocks. A
driver should not refuse old firmware; on `0x24` it can still point to
Dell's ControlVault2 package.

## Transport

Each command is a request/response exchange on interface 0:

1. Host writes the whole request to **bulk OUT `0x01`**.
1. Chip sends an 8-byte message on **interrupt IN `0x85`**: `u32 type`, `u32 length`.
   - `type = 0`: a reply of `length` bytes is ready.
   - `type = 3`: finger event (see [Capture](#capture-and-finger-events)); `length = 0`.
2. Host reads `length` bytes from **bulk IN `0x81`**. The stock driver reads
   with a 4196-byte buffer. The interrupt length can exceed the message
   (commit: interrupt 65, header 64, 65 bytes read); trust the header's total
   length.

One command is outstanding at a time.

## Message format

All integers little-endian.

### Header (0x2c bytes)

| Offset | Size | Request | Reply |
|---|---|---|---|
| 0x00 | 4 | `1` | `1` |
| 0x04 | 4 | total message length (header + params) | same |
| 0x08 | 2 | command ID | same command ID |
| 0x0a | 2 | flags (below) | request flags with bit 8 set (`0x0440` → `0x0540`) |
| 0x0c | 4 | `2` | `2` |
| 0x10 | 4 | session handle, or 0 (per command) | session handle |
| 0x14 | 4 | 0 | **status** (0 = success) |
| 0x18 | 16 | 0 | 0 |
| 0x28 | 4 | length of the parameter area | same |

Flags seen in plaintext mode: `0x0440` (ordinary command), `0x0442` (commands
`0x66`, `0x2f`, `0x0a`). Replies to failed commands are header-only (0x2c bytes).

Broadcom's public Citadel SDK ([sniten/citadel_sdk_2.1.1](https://github.com/sniten/citadel_sdk_2.1.1), Apache-2.0; `cvinternal.h`) names the
header fields (transport type 1 = encapsulated command, length, command ID,
flags, version, session, return status, a 16-byte IV at `0x18`, parameter
length) and the flag bits, which match every capture:

| Bit | Meaning | Seen |
|---|---|---|
| `0x0001` | secure session | Windows' protected traffic |
| `0x0002` | completion callback (asynchronous) | our `0x0442` commands |
| `0x0040` | USB transport | every message |
| `0x0180` | return type | replies (`0x0100`) |
| `0x0200` | suppress UI prompts | Windows (`0x0241`/`0x0243`) |
| `0x0400` | spare | set by the stock Linux driver |

### Parameters

The parameter area is a sequence of:

```
u32 kind | u32 length | data[length], zero-padded to a 4-byte boundary
```

| Kind | Observed use |
|---|---|
| 0 | fixed-size value or blob (handles, IDs) |
| 1 | string (open) |
| 2 | variable-length block: `0x2f` handle list, `0x6e`/`0x0a` attribute and authorization blocks (empty from the stock Linux driver); output slot |
| 3 | in/out buffer: only the length is sent (the size offered for an output), no data |

The kinds are the SDK's encapsulation types (0 structure, 1 length-prefixed
structure, 2 length/value pair, 3 in/out length/value pair). A kind-2 or kind-3
parameter supplies two of a command's arguments (length and value), which is
how the SDK's argument lists line up with our captured messages: for example
`0x2f` is session, a fingerprint-control value (`0x48`), a signed integer
setting (`0x53e2`), an object handle (0) and the handle list.

A request with a declared length that doesn't match the bytes sent is rejected
with status `0x0d`.

## Commands

| ID | Name (behavioral) | Header handle | Request params | Reply params (on success) |
|---|---|---|---|---|
| `0x02` | open session | 0 | `0:4 = 0x44`, `1:7 "myAppID\0"`, `1:8 "myUserID"`, `1:0` | `0:4` session handle (also in header +0x10) |
| `0x04` | close session | 0 | `0:4` session handle | — |
| `0x39` | get version | 0 | — | `1:n` version text (see [Firmware](#firmware-requirement)). Plaintext request: flags `0x0440`, header handle 0, param `0:4 = 0` (tested 2026-10-02 with `cvtool.py version`). The Windows driver sends flags `0x0040` |
| `0x82` | sensor reset (behavioral name) | 0 | `0:4 = 0`, flags `0x0040` | — (status 0 after ~0.7 s). Sent by the Windows driver when its Fingerprint settings page opens and when the device is (re)initialized, which is also before enrollments; see [Degraded state](#degraded-state-after-suspend) and [Windows flows](#windows-flows) |
| `0x8a` | begin enrollment | 0 | `0:4 = 0` | — (Windows: before every capture; stock Linux driver: once) |
| `0x66` | capture start | session | `0:4` handle, `0:4 = 2`, `0:4` mode: `0x23` before every sample (enroll and verify); `0x48` only directly after a `0x2f`, see [Verify](#verify) | `0:20` capture ID |
| `0x68` | capture cancel | 0 | `0:4 = 0` | — |
| `0x6c` | enrollment sample | session | `0:4` handle, `0:20` capture ID, `2:0` | `0:4` done flag (low byte), `0:20` ID, `0:4` (0) |
| `0x6e` | commit enrollment | session | `0:4` handle, `0:20` result ID, `2:8` attributes, `2:21` authorization (stock Linux driver: `2:0`, `2:0`), `3:0` | `3:0`, `0:4` **template handle** |
| `0x6d` | discard enrollment | 0 | `0:4 = 0` | — |
| `0x2f` | match | session | `0:4` handle, `0:4 = 0x48`, `0:4 = 0x53e2`, `0:4 = 0`, `2:4n` template handle(s) | `0:4` match (1/0), `0:4` matched template handle (0 if none) |
| `0x0a` | delete template | session | `0:4` handle, `0:4` template handle, `2:21` authorization (stock Linux driver: `2:0`) | — |

Attributes: `00 00 04 00 04 00 00 00`. Authorization: `01 01 ff 00 00 00 0d 00
0c` + `"BroadcomWBF\0"` (21 bytes). Tested 2026-10-03 on `00412015`: commit and
delete with both blocks succeed, templates committed with them match, and a
delete with the authorization also removes a template committed with empty
blocks.

Template handles are 32-bit values assigned by the chip at commit (observed:
`0x00dfa007`, `0x00785a80`). They persist across sessions and must be stored by
the host with each print. The meanings of the `0x2f` constants `0x48` and
`0x53e2` are unknown; they were identical in every capture.

### Capture and finger events

After `0x66` returns a capture ID, the chip sends an interrupt message with
`type = 3` when a finger is placed. The host then sends the sample command
(`0x6c` for enrollment) carrying that capture ID.

There is no finger-removed event. The stock driver starts the next capture
immediately after each sample; the chip raises one `type 3` event per press.

## Sequences

### Enrollment (observed, works on `00412015`)

```
0x02 open                        → handle
0x8a begin enrollment
repeat:
    0x66 capture start (0x23)    → capture ID     (0x85 → send 0x68 cancel, retry)
    wait for interrupt type 3
    0x6c sample(capture ID)      → status 0: done flag, ID
                                 → status 0x59: rejected sample, repeat
until done flag == 1
0x6e commit(ID from final 0x6c) → status 0
0x04 close
```

- On the final accepted sample, the ID returned by `0x6c` differs from the
  capture ID that was sent. The commit carries this final ID. On earlier
  samples the reply echoes the capture ID.
- Firmware `00412015`: 4 accepted samples. Firmware `00412001`: 13 accepted
  samples, then commit (empty blocks) fails with `0x24`.
- Dell's Windows driver sends `0x8a` before every capture
  (`windows-captures/03.pcap`), each followed by a protected session per
  sample. Sending `0x8a` before every capture on the plaintext interface
  (tested 2026-10-03) still completes in 4 samples; the open driver does so.
- Abort path: `0x68` cancel, `0x6d` discard, `0x04` close.

### Verify

```
0x02 open                        → handle
0x66 capture start (0x23)        → capture ID
wait for interrupt type 3
0x2f match(template handle)      → status 0: match flag, matched handle
0x66 capture start (0x48)        → status 0x47   (always, in every capture)
0x68 capture cancel
0x04 close
```

- Match and no-match both return status 0; the result is the first reply
  parameter (`1` = match, `0` = no match), and the second is the matched
  template handle, or 0.
- Observed: enrolled finger → `1` + its handle; a different enrolled finger
  checked against the other's template → `0`; an unenrolled finger → `0`.
- With no template handles (`2:0`), `0x2f` returns status `0x47` (seen in
  fprintd's pre-enroll duplicate check).
- The match takes ~0.2–0.35 s after the request.
- fprintd checked one template per verify in every capture.
- **Identify works:** one `0x2f` with several handles in the `2:4n` list
  returns `1` plus the handle of the finger that matched, or `0` + `0`.
  Tested 2026-10-02 with `cvtool.py match` and two templates: right index →
  its handle, left index → its handle, thumb → no match (`../cvtool.log`).
- Skipping the post-match `0x66` (mode `0x48`) call caused no problems in
  those tests.
- **Deleted handles:** if any handle in the list is not on the chip, `0x2f`
  fails with `0x1b` for the whole list. The capture stays usable: sending
  `0x2f` again on the same capture (no new `0x66`) with only valid handles
  matches normally, and can be repeated. `0x2f` with no capture pending
  returns `0x89` for valid and deleted handles alike, so there is no way to
  check for a handle without a capture (tested 2026-10-02,
  `cvtool.py stale-probe`).

### Delete

```
0x02 open
0x0a delete(template handle)     → status 0, header-only reply (~1.4 s)
0x04 close
```

One `0x0a` per template; fprintd opens a new session for each.

### Duplicate check before enrollment

fprintd runs a [Verify](#verify) sequence against all stored handles before
enrolling, then closes the session and opens a new one for the enrollment.

## Enumerate (`0x0d`)

Tested 2026-10-04 with cvtool `enumerate` (read-only). The Citadel SDK names
`0x0d` `CV_CMD_ENUMERATE_OBJECTS` (arguments: session, object type, and an
output buffer for the handle list; fingerprint objects are type 7), and its
numbering matches every command ID found first-hand here (`0x02`, `0x04`,
`0x0a`, `0x2f`, `0x39`, `0x66`, `0x68`, `0x6c`, `0x6d`, `0x6e`).

Request: `0:4` session, `0:4` type, `3:N` (buffer offer). On this reader
(firmware `00412015`), every attempt fails at once (about 10 ms) with status
**`0x100015`**, for object types 1, 7, 8 and 10, buffer sizes 0, 16 and 1024,
and with "suppress UI prompts" set at open (option `0x08`), in the header
(`0x0200`), or both. It is not `0x57` (unknown command), so the chip
implements `0x0d` but refuses it in this plaintext session. `0x100015` is in
the SDK's "user interface" return-code range (`0x00100000` mask). The SDK's
host-library copy of the status list (`load_sbi/inc/common/cvapi.h`), the
newest of its three copies, names it `CV_UI_FAILED_EVENT`; the same list
gives `0x85` = `CV_FP_DEVICE_BUSY` and `0x8d` = `CV_NO_VALID_FP_TEMPLATE`,
which match their observed meanings. The firmware's own copy ends at
`0x100014`. The firmware source of `cv_enumerate_objects` is not in the SDK,
so why it refuses is still unknown. Also refused: types 0, 2–6, 9, 11 and 12
(2026-10-04 21:30).

### Enumerate direct (`0x71`)

The SDK's `CV_CMD_ENUMERATE_OBJECTS_DIRECT`. Its dispatcher (`cvmanager.c`)
calls `cv_enumerate_objects_direct(u32, u8 *, type, &len, list)` with the
same in/out buffer as `0x0d`; the first two arguments are not documented.
Tested 2026-10-04 with cvtool `enumerate-direct` (firmware `00412015`, inside
an open session, header flags `0x0440`, buffer offer 1024):

| First argument(s) | Type | Result |
|---|---|---|
| one kind-2 pair: session handle, `myAppID\0`, `myUserID` or empty | 0–16 | status 0, `3:0` (empty list), ~10 ms. Types 13–16 don't exist, so the lookup isn't really happening |
| `0:4` 0, 1 or 7, then `0:4` session handle | 1, 7 | status 0, empty list |
| `0:4` 8, then `0:8` `myAppID\0` or `myUserID` | 7 | status 0, empty list |
| **`0:4` session handle, then `0:4` session handle** (or `0:8 myAppID\0`) | **7** | **status 0, `3:1024` holding 8 distinct 24-bit handles** (`0036f0b7 0086c7ad 0038aa55 00bcb3d1 006588a6 009e414a 00309409 00568474`), the 32-byte pattern repeated to fill the buffer |
| `0:4` session handle, then `0:4` session handle | 1 | **no reply; the chip hung** |

The list is real: it contains both templates fprintd holds for this laptop's
enrolled fingers (`0038aa55`, `006588a6`). The other six are probably Windows
Hello enrollments from the VM and leftovers. The first argument appears to be
the session handle (a firmware heap pointer); the second didn't matter for
type 7.

Open: why the 8 handles repeat to fill the whole buffer instead of the reply
length being 32 (a firmware bug, or the length is not clamped, which would
also explain a crash); whether the list is complete; and why type 1 hangs.

**Hazard:** after the type-1 call the chip stopped answering, even `0x39`. A
USB port disable/enable did not help: it then failed USB enumeration
(`device descriptor read/64, error -110`) until power-cycled. Don't send
`0x71` with any type other than 7, and treat even type 7 as risky until the
repetition is understood.

## Template storage

Tested 2026-10-04 (cvtool `commit-fill` and timed `delete`, `cvtool.log`).

- **Handles are 24-bit and look random.** All 18 handles committed in our
  captures and logs have a zero top byte and no order or step:
  `00785a80 007cfa75 00afe6fb 0000b1c8 00a7d371 00740102 001dbdae 00a2dc0d
  006e718d 0033637c 00f92683 00e07c5e 00e480f9 00946897 00264032 00fd0b40
  00490d81 001cba41` (in commit order; a tester's chip gave `006468fb`,
  `0054720d`).
- **A commit consumes the enrollment.** Committing the same result ID a
  second time fails with `0x8d`, so every template needs its own full
  enrollment; the chip can't be filled quickly from one.
- **No list command** has been seen in any capture, Linux or Windows; fprintd
  lists prints from its own storage, and so does Windows (see
  [Windows flows](#windows-flows)). Dell states only that readers with
  ControlVault hold "more than 10" enrollments (KB 000370873). Capacity and
  behavior when full are unknown; nobody has reported reaching it.
- **Missing handles answer fast:** `0x0a` on a handle not on the chip returns
  `0x1b` in about 14 ms, with or without the authorization block (the chip
  checks existence before authorization). `0x2f` against a held capture
  returns `0x1b` in about 40 ms. A real delete takes about 1.4 s.
- **Brute-force clearing is possible but blunt.** Deleting every handle in the
  24-bit space would take about 65 hours (16.7M × 14 ms), in resumable
  chunks, with the reader unusable meanwhile. It would remove every template,
  including the user's and probably Windows Hello's (Windows uses the same
  authorization), and `0x0a` may be a generic object delete that also removes
  other ControlVault objects. Finding templates without deleting them (match
  each handle against one held capture) takes about 8 days. Neither is meant
  for the driver; it is a last-resort recovery idea.

## Windows flows

Captured 2026-10-04 in the Windows VM (Windows Hello, Dell ControlVault2
package 4.12.11.15), one flow per capture: `windows-captures/05`–`13`
(git-ignored, like the other Windows captures). Windows talks through its
protected session, so only command IDs, flags, sizes, statuses and timing are
readable, plus the few plaintext commands.

### Protected session

Before each protected step Windows runs `0x23` (24 bytes in, a 152-byte reply
that assigns a session handle) and `0x24` (128 high-entropy bytes), then opens
a session with `0x02`. Protected messages carry random-looking bytes at header
`0x18`–`0x27` (zero in plaintext mode) and a high-entropy payload. Two flag
variants were seen: `0x0241`/`0x0243` (enrollment, identify) and
`0x0041`/`0x0043` (delete); bit `0x0001` is "secure session" and `0x0200`
"suppress UI prompts" (see [Header](#header-0x2c-bytes)).

### What each user action sends

| Flow | Capture | Commands (besides the protected-session setup) |
|---|---|---|
| Open Settings → Fingerprint | `05` | **`0x82` only.** Windows lists enrolled fingers from its own records; it does not ask the chip |
| Enroll, account has no fingers | `13` | `0x39`, then (`0x8a` `0x66` `0x6c`) ×4, `0x6e` |
| Enroll, account already has fingers | `06`, `13` | first touch: `0x66` → `0x73` (identify, duplicate check); then `0x39`, (`0x8a` `0x66` `0x6c`) ×4, **`0x2f` after the last sample** (second duplicate check), `0x6e` |
| Cancel an enrollment | `06` | `0x68`, `0x6d` (same as the open driver's cleanup) |
| Remove (all of the account's fingers) | `07` | `0x82`, then **one `0x0a` per finger** (2 fingers → 2 deletes). No bulk command |
| Disable/enable the devices in Device Manager | `08` | `0x82`, then `0x82` and `0x39` ×2 on re-enable. Nothing else |
| Sign in / unlock | `09`, `11` | `0x66` → `0x73` (identify) → `0x66` → **`0x9a`** → … → `0x68` |
| Failed attempts, then success | `12` | `0x73` per attempt; **`0x9a` only after the successful one** |
| List enrollments through the WinBio API (`WinBioEnumEnrollments`) | `14` | **`0x82` only** (when the client connects); the answer comes from Windows' database file |
| Open the device's Dell "Versioning" property tab | `15` | `0x39` only (it shows the firmware version) |
| Delete Windows' database file, restart the service | `16` | `0x82` only. Windows creates an empty database and never asks the chip; the API then reports no enrollments (`WINBIO_E_UNKNOWN_ID`) and the lock screen offers no fingerprint option |
| Sign in with a PIN | `16` | **`0x9a`** (no fingerprint involved) |

So Windows has no list, enumerate or clear command in its flows: it keeps its
own list of template handles and deletes them one at a time, and does not
rebuild that list from the chip when it is lost.

### `0x9a`

Plaintext (flags `0x0040`, no session), one `u32` parameter, sent once after
each **successful sign-in or unlock, by fingerprint or by PIN** (6 of 6
captures; capture `16` was a PIN sign-in with no fingerprint option offered),
never after a failed identify or an enrollment's duplicate check. The
parameter differs every time (`0x2b4c125e`, `0xf1eec6a1`, `0x08ec4787`,
`0x419f2dbb`, `0x196869de`, `0x0b53b38a`), so it is not a user or template ID.
It always fails with status **`0x57`** in about 13 ms, and Windows carries on.
In the Citadel SDK, `0x57` is `CV_INVALID_COMMAND`: this chip does not
implement `0x9a`.
Behaviorally: a "user signed in" notification with a fresh value, unrelated
to matching, which this chip refuses. The open driver does not send it.

### Windows host side

Read-only inventory of the installed package (4.12.11.15) and the running
system, 2026-10-04:

- **Components:** the `cvusbdrv` driver (the ControlVault device), the
  Windows Hello driver and its engine/sensor/storage plug-ins, and three
  services: "Credential Vault Upgrade Service", "Host Control Service for
  Fingerprint Processing" and "Credential Vault Host Storage — Host Storage
  Service for Persisting CV Objects into Hard drive". No management or
  diagnostic tool. Dell's only Device Manager tab ("Versioning") shows the
  firmware version (`0x39`). The package's driver also claims `0a5c:5833`
  with the same description as `5834`.
- **Host storage:** `ProgramData\Broadcom\HostStorage` holds four small files
  (`appIDs.dat` 276 B, `userSIDs.dat` 72 B, `userSIDxr.dat` 2480 B,
  `sess.dat` 12 B). Their sizes did not change with enrollments, so they are
  app/user/session bookkeeping, not templates; templates stay on the chip.
- **Windows' database:** one file per biometric database
  (`WinBioDatabase\EFBD8DB5-….DAT` for Dell's), holding the user ↔ template
  records that Settings, the API and the lock screen read.
- **Logging:** no Dell trace providers or event logs. Windows'
  `Microsoft-Windows-Biometrics/Operational` log shows, before re-enrolling,
  "failed to delete a database record" with `WINBIO_E_DATABASE_NO_SUCH_RECORD`
  (Windows clearing its own records, not the chip's), and a "secure component"
  that cannot start in a VM (Enhanced Sign-in Security; unrelated).

### Other commands seen

| IDs | Where | Likely role |
|---|---|---|
| `0x36` ×28 (1–4 KB), `0x4b` | firmware update, stage 1 (done twice; the chip re-enumerates as `0a5c:5831`) | image upload, then commit or reboot |
| `0x43`, `0x44` ×300, `0x45`, `0x4c` | firmware update, stage 2 | header, encrypted data, finalize, activate. Talos names `CV_CMD_FW_UPGRADE_START`/`_UPDATE`/`_COMPLETE` for ControlVault3; the shape matches, the IDs are an inference |
| `0x73` | identify | protected match (the plaintext interface uses `0x2f`) |

## Degraded state after suspend

Observed 2026-10-02: after a suspend/resume the reader got into a state where
enrollment needed many more accepted samples (10 to 38+, often never
finishing, with a repeating "3 accepted, 1 rejected `0x59`" pattern),
half-covered presses were accepted, and verify never matched, including with
the stock Linux driver. Reboot, a power-button drain with AC unplugged,
`0x68`/`0x6d` and wiping the sensor did not help; Windows Hello still worked.
Sending `0x82` once (as the Windows driver does) restored normal behavior:
4-sample enrollment and matching. A second `0x82` kept stored templates
(verify still matched). The open driver sends `0x82` at device open.

## Status codes

| Code | Seen on | Observed meaning | Handling |
|---|---|---|---|
| `0x00` | all | success | — |
| `0x59` | `0x6c` | sample rejected (poor or partial press); header-only reply | ask for another press |
| `0x85` | `0x66` | capture already pending; seen on the first `0x66` of every new client after another one exited | `0x68` cancel, retry `0x66` |
| `0x57` | `0x9a` | always, after each successful Windows sign-in (see [`0x9a`](#0x9a)); SDK: `CV_INVALID_COMMAND` | not sent by the open driver |
| `0x100015` | `0x0d` | enumerate refused in a plaintext session (see [Enumerate](#enumerate-0x0d)) | not sent by the open driver |
| `0x8d` | `0x6e` | no completed enrollment to commit (also a second commit of the same result) | error |
| `0x0d` | `0x6e` | malformed request; pending enrollment kept (SDK: `CV_PARAM_BLOB_INVALID_LENGTH`) | bug |
| `0x24` | `0x6e` | firmware `00412001`, commit with empty blocks: rejected, enrollment consumed (SDK: `CV_OBJECT_ATTRIBUTES_INVALID`) | suggest firmware update |
| `0x1b` | `0x2f`, `0x0a` | template handle not on the chip (deleted); tested 2026-10-02 (SDK: `CV_INVALID_OBJECT_HANDLE`) | print not found |
| `0x47` | `0x2f`, `0x66` | `0x2f` with no template handles; `0x66` (mode `0x48`) after every match (SDK: `CV_INVALID_INPUT_PARAMETER`) | no match / expected, ignore |

## Security model

Match-on-chip: templates are stored and matched inside the chip, and no
fingerprint images or templates cross USB. The plaintext interface does not
authenticate replies, so the host trusts the USB device's match result. This is
the same posture as other match-on-chip drivers in libfprint. It is weaker than
the vendor's Windows stack, which uses a protected session.

## Open questions

Answered by `captures/` and `cvtool.log` (2026-10-02): commit returns a
template handle; match command and result format; identify with several
handles; delete command.

Still open:

1. Template capacity and behavior when full (the SDK has
   `CV_OBJECT_DIRECTORY_FULL`, `0x26`); why enumerate (`0x0d`) is refused
   with `0x100015` in a plaintext session (see [Enumerate](#enumerate-0x0d)).
2. Purpose of the `0x66` mode `0x48` call after a match, and of the `0x2f`
   constants `0x48` and `0x53e2`.
4. Meaning of the `0x02` open parameters (`0x44`, app/user strings) and of
   `0x66` parameters 2 (`2`) and 3 (mode: `0x23` vs `0x48`).
5. Whether `0x8a` before each sample matters on any firmware (Windows sends
   it; the stock Linux driver doesn't; both work on `00412015`).
6. Whether `00047026` (and other firmware before `00412001`) enrolls with the
   commit's attribute and authorization blocks, and what those bytes mean.
7. What `0x9a` reports after a sign-in (this chip does not implement it:
   `0x57` is `CV_INVALID_COMMAND`; see [`0x9a`](#0x9a)).
