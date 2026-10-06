# CV2 fingerprint project: handoff

Status as of 2026-10-02. Read this first, then `protocol/PROTOCOL.md`.

## Goal

An open-source **libfprint match-on-chip driver** for the Dell ControlVault2 /
Broadcom BCM5880 fingerprint reader (USB `0a5c:5834`, Dell Latitude 7490 and
others), submitted **upstream to libfprint**.

## Current system state (this laptop)

- Fingerprint login runs on the **open cv2 driver**: the libfprint build from
  `~/projects/libfprint` (branch `cv2-driver`) is installed in
  `/usr/local/lib64`, and fprintd uses it through the drop-in
  `/etc/systemd/system/fprintd.service.d/cv2-dev.conf`
  (`LD_LIBRARY_PATH=/usr/local/lib64`). Revert with
  `pkexec bash devtools/uninstall-dev.sh` (fprintd then uses Fedora's
  libfprint, which has no driver for this reader).
- The patched TOD stopgap is uninstalled (Fedora `libfprint`, `fprintd`,
  `fprintd-pam` reinstalled). Its files remain in `~/projects/cv2-fingerprint`.
  Note: its `uninstall-fedora.sh` also removes fprintd and fprintd-pam.
- `authselect` feature `with-fingerprint` is enabled.
- ControlVault firmware: `USH_REL_UPGRADE_VER 00412015`.
- Enrolled: right and left index (fprintd storage + chip).

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

The driver is done and reviewed on `~/projects/libfprint` branch `cv2-driver`
(13 commits on upstream master 6f9479c; execution log with every ruling:
`docs/superpowers/plans/2026-10-02-cv2-libfprint-driver-execution-log.md`).

**Upstream submission:** merge request
https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/672
(opened 2026-10-03 from your fork (`<user>/libfprint`) branch `cv2-driver`, maintainers
may push to it). `glab` is installed and logged in with your account on
gitlab.freedesktop.org; the fork remote in `~/projects/libfprint` is `fork`.
Links posted on grosa787/dell-controlvault2-fingerprint-linux PR #13 and
issue #3. 2026-10-03: series reworked after a code review (cloud session) and
force-pushed as badc1603: 13 commits, all authored by Matt; new recording
`tests/cv2-retries` (short touch, rejected sample, stale print first, every
print missing).
2026-10-03 (later): two commits added on top, pushed as 466c9c43 (no
rewrite): commit/delete send the attribute and authorization blocks
(captured from PR #13's patched TOD driver, `captures/08-tod-pr13-commit-delete.pcap`),
0x8a before every enrollment capture (as Windows does), and old firmware is
tried instead of refused. Policy: emulate the vendor's captured traffic as
closely as possible. MR description and a comment updated; testers on old
firmware requested on GitHub issue #3, firmware version asked on PR #13.
Installed locally (`install-dev.sh`). Match rate on this sensor is modest
(roughly 1–3 of 4 in tests; same bytes as the old build), so a missed
match is not by itself a regression.
2026-10-04: a tester's Precision 3520 on firmware `00412001` passed enroll,
verify, wrong-finger rejection and delete with the open driver (issue #3),
so the blocks make old firmware work without an update. Next: rethink the
driver's old-firmware messages/gate (no longer needed as a warning), then
update the MR; respond to maintainer review; CI may need a maintainer to
start it.
2026-10-05: list added to the MR (pushed as 80c7f744, fast-forward, four
commits: list with our owner hash SHA-1("myAppIDmyUserID"), DATA_FULL for
0x25/0x28, clearer 0x24 message, tests/cv2 recording with list). Owner
hash confirmed on the reader with Windows Hello templates present; list
tested through fprintd; MR description (from `docs/upstream-mr.md`) and a
comment updated. Session strings stay `myAppID`/`myUserID` (every Linux
driver uses them). Installed build: `cv2-list` worktree
(`.claude/worktrees/cv2-list`, branch `cv2-list-squash` = MR head). Next:
wait for maintainer review / CI.
Independent test: Latitude 7480 by a tester on the GitHub issue (all operations
pass; offered to test further revisions). Firmware package facts are in the
MR comments and README.
After it lands, the wiki's Unsupported-Devices entry for `0a5c:5834` must be
removed (noted in the MR).

Findings from implementation (in `protocol/PROTOCOL.md` and the spec's
amendments): the chip can enter a degraded state after suspend that `0x82`
fixes; brief touches give finger events with length 7; deleted handles make
`0x2f` fail with `0x1b` for the whole list; `0x89` means no capture.
