# CV2 fingerprint project: handoff

Status as of 2026-10-02. Read this first, then `protocol/PROTOCOL.md`.

## Goal

An open-source **libfprint match-on-chip driver** for the Dell ControlVault2 /
Broadcom BCM5880 fingerprint reader (USB `0a5c:5834`, Dell Latitude 7490 and
others), submitted **upstream to libfprint**.

## Current system state (this laptop)

- Fingerprint login **works** through a stopgap: the patched closed Broadcom TOD
  driver (`prebuilt/libfprint-2-tod-1-broadcom.PATCHED-v2.so`, built by
  `patch_driver_v2.py`) installed at `/usr/lib64/libfprint-2/tod-1/`, with
  `libfprint-tod` + `libfprint-tod-selinux` from COPR
  `grahamwhiteuk/libfprint-tod`. Reinstall: `try-v2.sh` / `install-fedora.sh`.
  Revert: `uninstall-fedora.sh`. These stopgap files live in
  `~/projects/cv2-fingerprint` (a clone of myrtree's patch repo).
- `authselect` feature `with-fingerprint` is enabled (password fallback works).
- fprintd debug logging drop-in is still installed
  (`/etc/systemd/system/fprintd.service.d/debug.conf`); remove when done.
- ControlVault firmware: `USH_REL_UPGRADE_VER 00412015` (updated from
  `00412001` by Dell's Windows package). Enrollment needs this version.
- Test templates `0036622f` and `005b831c` were deleted from the chip
  (`cvtool.log`, 2026-10-02 12:07).
- Check that a login finger is enrolled: `fprintd-list $USER`.

## Files

| Path | What |
|---|---|
| `protocol/PROTOCOL.md` | **Protocol spec** (from USB captures only): transport, framing, commands, enroll/verify/identify/delete sequences, status codes, security model, open questions |
| `protocol/captures/*.pcap` | usbmon captures per phase (verify match/no-match, enroll, identify, delete), Linux plaintext interface |
| `enroll-full.pcap`, `enroll-usbmon*.txt` | earlier enroll captures (old firmware) |
| `windows-captures/` | Windows VM captures (firmware update, enroll, login). Windows uses a protected session; **not** used for the driver |
| `cvtool.py` + `run-cvtool.sh` | Python research tool that drives the chip directly (allow-listed commands only). Actions: `enroll`, `match --handles`, `delete --handles`, `commit-noenroll` |
| `cvtool.log` | log of every cvtool run, including the identify test |
| `cvdump.py` | decodes CV traffic from usbmon pcaps |
| `github-comment.md` | comments already posted on upstream PR #13 / issue #3 |
| `docs/superpowers/specs/` | driver design spec |

This repo (`~/projects/cv2-research`) holds the research and design. The
patched-TOD stopgap (`prebuilt/`, `patch_driver*.py`, `PATCHES.md`,
`install*.sh`, `try-v2.sh`) stays in `~/projects/cv2-fingerprint`, a clone of
myrtree's repo; it is not used for the open driver. `windows-captures/` is
git-ignored: it contains the proprietary firmware-update transfer.

## Agreed design decisions

1. **Target:** upstream libfprint (freedesktop GitLab), following its driver
   conventions (GObject C, `FpiSsm`, umockdev replay tests). Protocol provenance
   is USB captures only; nothing from the proprietary binary.
2. **Structure (approach A):** a protocol layer plus a device layer.
3. **Features v1:** enroll, verify, **identify** (tested: one `0x2f` with several
   handles returns the matching handle), delete. No on-chip list/clear.
4. **Storage:** each print stores the chip-assigned 32-bit template handle in its
   driver data; fprintd's storage is the source of truth.
5. **Old firmware (< `00412015`):** device still probes; verify/identify/delete
   work; enroll refuses with a clear "update ControlVault firmware via Dell's
   package" error. If the version can't be read, map `0x24` at commit to the
   same error.
6. **Scope:** fingerprint commands on interface 0 only; never touch the
   smart-card interfaces or firmware update.
7. **Security model:** standard plaintext match-on-chip posture, documented
   (see `PROTOCOL.md`).

## Design section 1 (approved): components and layout

- Code in a libfprint fork at `~/projects/libfprint`, branch `cv2-driver`,
  directory `libfprint/drivers/cv2/`, registered in `meson.build`.
- `cv2-proto.h/.c`: request builders and reply parser (header, parameters,
  status, typed getters for done flag / ID / template handle / match result),
  version-string parser. GLib only; unit-testable against capture bytes.
- `cv2.h/.c`: `FpiDeviceCv2`: ID table `0a5c:5834`, features
  `VERIFY | IDENTIFY | STORAGE | STORAGE_DELETE`, scan type press, 4 enroll
  stages, the transport, and one `FpiSsm` per operation.
- `tests/cv2/`: umockdev recordings for enroll, verify, identify, delete;
  `tests/test-cv2-proto.c` unit tests.

## Where to resume

Design is complete (sections 1–4). The spec is at
`docs/superpowers/specs/2026-10-02-cv2-libfprint-driver-design.md`. Next:
written-spec review, then an implementation plan (superpowers writing-plans).

Open protocol questions that affect implementation are listed in the spec's
"Implementation prerequisites and risks" and at the end of
`protocol/PROTOCOL.md` (notably the plaintext form of the `0x39` version
query).

Build dependencies are not installed yet (meson, gcc, glib2-devel,
libgusb-devel, gobject-introspection-devel, umockdev-devel, …).
