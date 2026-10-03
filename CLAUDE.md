# CV2 fingerprint driver: working context

Open libfprint driver `cv2` for the Dell ControlVault2 / Broadcom BCM5880
fingerprint reader (USB `0a5c:5834`) in Matt's Latitude 7490 (Fedora 44,
KDE Plasma). Status and next steps: `HANDOFF.md`. Read it first.

## Repositories

| Path | What | Rules |
|---|---|---|
| `~/projects/cv2-research` (this repo) | protocol notes, captures, tools, spec, plan, execution log, MR text | **Public** on GitHub as `dell-controlvault2-fingerprint-research` (remote `origin`, CC0). Keep usernames and `windows-captures/` (Dell firmware) out of it |
| `~/projects/libfprint` branch `cv2-driver` | the driver; remote `fork` = `your fork of libfprint on gitlab.freedesktop.org` | Upstream MR !672. Its `CLAUDE.md` is excluded via `.git/info/exclude`; never commit it |
| `~/projects/cv2-fingerprint` | old patched-TOD stopgap (clone of someone else's repo) | Don't build on it. Its `uninstall-fedora.sh` also removes fprintd/fprintd-pam |

Key documents here:
- `protocol/PROTOCOL.md`: full protocol notes with dates and tools.
- `docs/upstream-protocol.md`: the cleaned spec that is embedded in the MR.
- `docs/superpowers/specs/2026-10-02-cv2-libfprint-driver-design.md`: the design spec. Its "Amendments" section overrides the earlier sections.
- `docs/superpowers/plans/2026-10-02-cv2-libfprint-driver-execution-log.md`: every ruling, finding and deferred item.
- `docs/upstream-mr.md`: the MR description as submitted.

## Upstream

- MR: https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/672
- `glab` is logged in with your account. Always set `GITLAB_HOST=gitlab.freedesktop.org`.
  - Read the MR: `glab mr view 672 --repo libfprint/libfprint`
  - Pipelines live in the fork project your fork (`<user>/libfprint`), for example `glab api projects/<user>%2Flibfprint/pipelines/<id>/jobs`.
- Push fixes: `git -C ~/projects/libfprint push fork cv2-driver`.
- CI facts:
  - Push pipelines on the fork always fail with a runner-gating "privileges" message. Only the MR pipeline counts.
  - `test_scan_build` fails on upstream master too (elanspi, generated introspection code) and is allowed to fail. Keep `cv2` at zero warnings; reproduce locally with `SCANBUILD=$PWD/.gitlab-ci/scan-build ninja -C _build scan-build` (`meson setup _build -Ddrivers=all`).
  - `test_unsupported_list` fails until a maintainer removes `0a5c:5834` from the wiki's Unsupported-Devices page. Already requested in the MR.
  - `egis_etu905` (an upstream test) is flaky and sometimes fails `test`/`test_asan`. Not ours.
- Commit style: `cv2: …` or `tests/cv2: …`. Run `scripts/uncrustify.sh` after each commit and amend if it changes anything. End every commit message with `Co-Authored-By: Claude <noreply@anthropic.com>`. The MR discloses AI assistance.

## Build and test (libfprint)

```
meson setup build -Ddoc=false              # once
meson compile -C build
meson test -C build cv2-proto cv2 cv2-cancel cv2-retries
```

- Avoid running the full suite casually. Upstream's `egis_etu905` aborts and makes the desktop show crash notifications.
- `cv2-cancel` is marked serial. Its replay stalls if the cancel is delayed or the machine is loaded: umockdev replays a cancelled interrupt read in recorded order.

## Hardware work

All of it needs root, through `pkexec`; Matt approves each prompt. Fingerprint is enabled in PAM, so the reader works for approval: press Enter on the empty password field, then touch it (see the PAM note below).

- `devtools/run-dev.sh <action>`: runs the build-tree driver with fprintd masked. Actions: `info`, `enroll <finger>`, `verify <finger>`, `identify`, `delete <finger>`, `verify-bogus`, `cancel-enroll`. It sends desktop notifications, since Matt can't see the terminal while pressing fingers. Batch several actions into one `pkexec bash -c` and tell him the press order beforehand.
- `devtools/record-tests.sh [cv2|cancel|retries]`: re-records the umockdev tests. Needed whenever the driver's USB traffic changes, for example the open sequence.
- `devtools/install-dev.sh` / `uninstall-dev.sh`: install the build to `/usr/local` for fprintd (drop-in `LD_LIBRARY_PATH`), or revert.
- `run-cvtool.sh <action>`: the raw protocol probe (`cvtool.py`, allow-listed commands only).
- `fprintd-verify` with no `-f` checks only the first enrolled finger. Use `-f any` to test the identify path the lock screen uses.
- PAM on this laptop is password-first everywhere except the lock screen: a typed password is accepted at once, and Enter on an empty field falls through to the reader. Background: `pam_fprintd` before `pam_unix` makes a typed password wait for the fprintd timeout (30 s), because a PAM stack runs one module at a time.
  - Boot greeter: `/etc/pam.d/plasmalogin` overrides the packaged `/usr/lib/pam.d/plasmalogin`. It replaces `auth substack password-auth` with `pam_env`, `pam_faildelay`, `[success=1 default=ignore] pam_unix.so nullok`, `requisite pam_fprintd.so`, `required pam_permit.so`. The final `pam_permit` is required: a jump records no result in the auth stack, so without it a correct password gets `PAM_PERM_DENIED`. The KWallet password is blank so a fingerprint login doesn't prompt for it.
  - sudo, polkit, pkexec: authselect profile `custom/pwfirst`, a copy of `local` with the `pam_fprintd` line moved below `pam_unix` (both `sufficient`). Undo: `authselect select local with-fingerprint with-silent-lastlog with-mdns4`.
  - Lock screen: unchanged. KDE runs the `kde` and `kde-fingerprint` stacks in parallel, so password and finger both work at any time.
- `devtools/pamtest.py SERVICE ask|empty|wrong`: runs a PAM service's auth stack as the current user, answering every password prompt like a greeter would, with timestamps. Use it to check a PAM change before logging out. `ask` reads the password with getpass.
- Windows VM `win11` (`virsh -c qemu:///system`) has the reader passed through. Starting it takes the reader away from Linux.

## Hard-won protocol facts

Details are in PROTOCOL.md.
- `0x82` (sensor reset, from the Windows driver) at open fixes a post-suspend state in which nothing matches and enrollment never finishes, with any driver. Reboots don't fix that state. `0x82` keeps stored templates.
- A finger event with a nonzero length (7) means the touch was too brief. `0x89` means no usable capture. Both are retries.
- `0x1b` means a template handle is not on the chip, and it fails the whole `0x2f` list. The capture can be re-matched one handle at a time.
- `0x85` happens on every new client's first capture. The fix is cancel and retry.
- Enrollment needs firmware `00412015` or newer (`0x39`, `USH_REL_UPGRADE_VER`).
- No biometric data crosses USB. The largest message is the 848-byte version text.
