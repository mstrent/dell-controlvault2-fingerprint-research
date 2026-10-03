# Dell ControlVault 2 (Broadcom BCM5880, USB `0a5c:5834`) fingerprint protocol

Status: **draft**. Covers the plaintext host interface the fingerprint function uses.

## Provenance

Everything here comes from **USB traffic captures** (Linux `usbmon`) of the
reader on a Dell Latitude 7490 owned by the author, while it was driven by
fprintd. Command names describe the **observed behaviour**; they are not taken
from vendor headers or binaries. Facts not yet observed are listed under
[Open questions](#open-questions) rather than guessed.

Out of scope: the reader's smart-card (CCID) interfaces, firmware update, and
the protected session mode used by the Windows driver. An open driver should
use only the fingerprint commands documented here.

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
| `USH_REL_UPGRADE_VER` | `00412001` (factory): enroll commit fails with `0x24` · `00412015`: enroll works |

A driver should read this at open and refuse to enroll on firmware older than
`00412015`, with a message explaining that the update comes from Dell's
ControlVault2 package.

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

### Parameters

The parameter area is a sequence of:

```
u32 kind | u32 length | data[length], zero-padded to a 4-byte boundary
```

| Kind | Observed use |
|---|---|
| 0 | fixed-size value or blob (handles, IDs) |
| 1 | string (open) |
| 2 | variable-length block, sent empty in all observed requests; output slot |
| 3 | buffer, sent with length 0 in observed commit requests |

A request with a declared length that doesn't match the bytes sent is rejected
with status `0x0d`.

## Commands

| ID | Name (behavioural) | Header handle | Request params | Reply params (on success) |
|---|---|---|---|---|
| `0x02` | open session | 0 | `0:4 = 0x44`, `1:7 "myAppID\0"`, `1:8 "myUserID"`, `1:0` | `0:4` session handle (also in header +0x10) |
| `0x04` | close session | 0 | `0:4` session handle | — |
| `0x39` | get version | 0 | — | `1:n` version text (see [Firmware](#firmware-requirement)). Plaintext request: flags `0x0440`, header handle 0, param `0:4 = 0` (tested 2026-10-02 with `cvtool.py version`). The Windows driver sends flags `0x0040` |
| `0x82` | sensor reset (behavioural name) | 0 | `0:4 = 0`, flags `0x0040` | — (status 0 after ~0.7 s). Sent by the Windows driver before each enrollment; see [Degraded state](#degraded-state-after-suspend) |
| `0x8a` | begin enrollment | 0 | `0:4 = 0` | — |
| `0x66` | capture start | session | `0:4` handle, `0:4 = 2`, `0:4` mode: `0x23` before every sample (enroll and verify); `0x48` only directly after a `0x2f`, see [Verify](#verify) | `0:20` capture ID |
| `0x68` | capture cancel | 0 | `0:4 = 0` | — |
| `0x6c` | enrollment sample | session | `0:4` handle, `0:20` capture ID, `2:0` | `0:4` done flag (low byte), `0:20` ID, `0:4` (0) |
| `0x6e` | commit enrollment | session | `0:4` handle, `0:20` result ID, `2:0`, `2:0`, `3:0` | `3:0`, `0:4` **template handle** |
| `0x6d` | discard enrollment | 0 | `0:4 = 0` | — |
| `0x2f` | match | session | `0:4` handle, `0:4 = 0x48`, `0:4 = 0x53e2`, `0:4 = 0`, `2:4n` template handle(s) | `0:4` match (1/0), `0:4` matched template handle (0 if none) |
| `0x0a` | delete template | session | `0:4` handle, `0:4` template handle, `2:0` | — |

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
  samples, then commit fails with `0x24`.
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

## Degraded state after suspend

Observed 2026-10-02: after a suspend/resume the reader got into a state where
enrollment needed many more accepted samples (10 to 38+, often never
finishing, with a repeating "3 accepted, 1 rejected `0x59`" pattern),
half-covered presses were accepted, and verify never matched, including with
the stock Linux driver. Reboot, a power-button drain with AC unplugged,
`0x68`/`0x6d` and wiping the sensor did not help; Windows Hello still worked.
Sending `0x82` once (as the Windows driver does) restored normal behaviour:
4-sample enrollment and matching. A second `0x82` kept stored templates
(verify still matched). The open driver sends `0x82` at device open.

## Status codes

| Code | Seen on | Observed meaning | Handling |
|---|---|---|---|
| `0x00` | all | success | — |
| `0x59` | `0x6c` | sample rejected (poor or partial press); header-only reply | ask for another press |
| `0x85` | `0x66` | capture already pending; seen on the first `0x66` of every new client after another one exited | `0x68` cancel, retry `0x66` |
| `0x8d` | `0x6e` | no completed enrollment to commit | error |
| `0x0d` | `0x6e` | malformed request; pending enrollment kept | bug |
| `0x24` | `0x6e` | firmware `00412001` only: commit rejected, enrollment consumed | require firmware update |
| `0x1b` | `0x2f`, `0x0a` | template handle not on the chip (deleted); tested 2026-10-02 | print not found |
| `0x47` | `0x2f`, `0x66` | `0x2f` with no template handles; `0x66` (mode `0x48`) after every match | no match / expected, ignore |

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

1. Listing templates stored on the chip; slot limit; behaviour when full.
2. Purpose of the `0x66` mode `0x48` call after a match, and of the `0x2f`
   constants `0x48` and `0x53e2`.
4. Meaning of the `0x02` open parameters (`0x44`, app/user strings) and of
   `0x66` parameters 2 (`2`) and 3 (mode: `0x23` vs `0x48`).
5. Whether `0x8a` is needed before each sample (it is sent once per enrollment
   on Linux).
