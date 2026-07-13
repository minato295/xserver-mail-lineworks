# Final whole-branch review fixes

## Status

Complete. All Critical, Important, and Minor findings in `.superpowers/sdd/final-review.md` were implemented without production deployment, push, or Mac app installation.

## TDD evidence

### RED

The focused Python regression command failed for all four newly asserted boundaries:

`python3 -m unittest tests.python.test_manager.ManagerTest.test_mask_uses_stars_only_for_short_tokens_and_last_four_for_long_tokens tests.python.test_manager.ManagerTest.test_main_reads_initial_private_config_over_ssh_when_ftps_retr_is_denied tests.python.test_private_config_ssh.PrivateConfigSshTest.test_malformed_response_types_are_redacted_without_exception_chain tests.python.test_release_workflow.ReleaseWorkflowTest.test_legacy_exact_fixed_runtime_adds_only_private_config_helper_atomically -v`

Observed failures were the old fixed mask, initial FTPS read returning startup code 2, an uncaught `AttributeError` for a `None` response, and rejection of the legacy exact runtime as partial.

The PHP helper regression command failed at the first new negative schema assertion:

`php tests/php/test_manage_private_config.php`

Observed failure: `invalid notifier schema must be rejected on read`.

### GREEN

Focused verification:

`php -l bin/manage-private-config.php && php tests/php/test_manage_private_config.php && python3 -m unittest tests.python.test_manager tests.python.test_private_config_ssh tests.python.test_release_workflow -v`

Result: PHP syntax and helper E2E passed; 100 Python tests passed.

Full verification:

`bash tests/run-all.sh`

Result: 367 Python tests passed, 1 existing opt-in real-Mac SSH test skipped; all PHP tests, syntax checks, compile checks, receipt verification, and public secret scan passed.

`git diff --check` also passed with no output.

## Finding resolution

- Legacy exact fixed runtime: recognizes only the old exact set, verifies it independently over SSH and FTPS, atomically installs only `manage-private-config.php` as 0700, binds the FTPS readback to exact bytes/mode, and requires the full new SSH set to become exact. Other partial states remain rejected; fresh and already-current behavior is unchanged.
- Initial configuration: `_run_main()` now reads through `PrivateConfigSsh` using the existing `[ftps_host, servername]` metadata before entering the menu. Existing FTPS-backed deployer methods remain available to their pre-existing operations. An integration regression executes menu 14 and 15 while FTPS config RETR is forced to fail.
- PHP CAS: an owner-only, non-symlink regular 0600 lock file is safely created with owner-only umask or strictly re-opened by identity, then held under `flock(LOCK_EX)` across read, compare, temp write, rename, readback, and conditional restore. A two-process E2E test requires exactly one `changed` and one `conflict` for distinct new values with the same expected hash.
- Masking: tokens longer than four characters render as `********…` plus the last four; shorter tokens render only same-length stars.
- SSH response parsing: non-bytes, decode failures, duplicate keys, and malformed JSON map to a fixed exception raised outside the parser exception context. Tests traverse exception cause/context/args/repr and find no fixture secret.
- Helper schema: both reads and replacements require string `webhook_url`, a non-empty list of valid string email addresses, and an absolute non-`public_html` log path, while preserving unknown keys and accepting the existing normal configuration shape.
- Directory trust: the account home must be owner-controlled and not group/world writable (without imposing 0700); `mail-lineworks` and `private` must be exact owner-only 0700 non-symlink directories. Identity snapshots are rechecked at transaction and rename boundaries. Intermediate symlinks and unsafe home modes are rejected.

## Secrets and operational scope

All added values are explicit placeholders. No Webhook URL or credential is added to argv, exceptions, logs, or public artifacts. No production operation, push, or Mac installation was performed.

## Concerns

None. `unchanged` remains valid only for the existing specification case where the requested complete new configuration already equals the current configuration; the concurrency regression uses distinct new configurations, so the second writer must classify as `conflict`.

---

# Final review fixes: round 2

## Status

Complete. The legacy bootstrap deadlock, remaining FTPS configuration mutation paths, concurrent-test synchronization, and PHP Webhook schema boundary were resolved without production deployment, push, or Mac installation.

## Root cause and bootstrap design

The installed Mac bundle previously contained the Python SSH client but neither the PHP helper bytes nor enough immutable metadata to prove that a server was exactly the known legacy fixed runtime. Therefore a failed initial SSH config read could not safely call `provision_fixed_runtime`, and menu 12 could never be reached.

The installer now generates and signs two exact owner-only bundle resources:

- `fixed-runtime/manage-private-config.php`
- `fixed-runtime/legacy-manifest.json`, containing the normalized legacy fixed-runtime directory/file type, mode, size, and SHA-256 inventory, excluding only the new helper.

The installer rebuilds these assets from verified no-follow source bytes, includes them in the exact bundle allowlist and tree receipt, and compares bundle bytes with regenerated expected bytes during validation. Existing exact pre-asset app layouts remain the only allowed upgrade layouts.

On an initial config read failure, the user must enter the exact Japanese bootstrap confirmation. `ReleaseWorkflow.provision_legacy_helper_assets` then requires the full new SSH inventory to be `PARTIAL` and the old inventory to be independently `EXACT`; verifies every old file over one protected FTPS connection by exact size, SHA-256, and mode; atomically adds only the helper as 0700; binds its FTPS bytes/mode readback; and requires the full new SSH inventory to become `EXACT`. General SSH failures propagate before any FTPS write. The config is then retried over SSH before the normal manager starts.

## SSH CAS unification

All `MailManager` configuration reads and writes now use its `private_config_client` abstraction. Target sync, error-recipient changes, diagnostics, LINE WORKS tests, scoped failure flags, and post-release metadata updates use fresh SSH reads with exact SHA-256, helper CAS, and full readback verification. `manager/manage.py` contains no `self.deployer.read_private_config` or `self.deployer.update_private_config` call. FTPS remains available for release bytes, bootstrap proof/readback, journals, and installer-era configuration only.

## PHP schema and concurrency test

The helper now accepts `webhook_url` only when it exactly matches canonical `https://webhook.worksmobile.com/message/<single-token>` syntax with the established token alphabet. Negative read and replacement tests cover scheme, nested path, and query variants without exposing values in errors.

The two-process CAS test now acquires the real owner-only helper lock as a parent-held barrier, starts both processes while the barrier is held, and releases both into the same `flock(LOCK_EX)` contention. Distinct proposed configurations with one expected hash must still produce exactly `changed` plus `conflict`.

## Verification

Focused command:

`php tests/php/test_manage_private_config.php && python3 -m unittest tests.python.test_macos_installer tests.python.test_release_workflow tests.python.test_manager tests.python.test_ftps_deployer -q`

Result: PHP helper E2E passed; 190 focused Python tests passed.

Full command:

`bash tests/run-all.sh`

Result: 372 Python tests passed, 1 existing opt-in real-Mac SSH test skipped; all PHP tests, syntax and compile checks, installer receipt verification, and public secret scan passed.

## Concerns

None. The bootstrap manifest intentionally represents the immediately preceding exact fixed runtime, whose only missing entry is the helper; any different, partial, modified, or unreachable runtime fails closed before mutation.

---

# Final review fixes: round 3

## Status and root cause

Complete. Direct repository execution and the installed app now resolve their own explicit bootstrap layouts, and bootstrap trust no longer derives expected hashes from the files being consumed. No production deployment, push, or Mac app installation was performed.

The round-2 repository path always selected `fixed-runtime/manage-private-config.php`, which exists only inside the app bundle. The installer also regenerated the legacy inventory from the same source tree used to create the asset, so runtime consumption had no independent pinned identity for either asset.

## Asset layouts and independent trust

The two supported layouts are exact and non-probing:

- repository CLI: tracked `bin/manage-private-config.php` plus tracked frozen `fixed-runtime/legacy-manifest.json`, both exact 0644;
- signed app: `Contents/Resources/fixed-runtime/{manage-private-config.php,legacy-manifest.json}`, both normalized to exact 0600 by the installer.

The helper and frozen manifest each have an independently pinned byte size and SHA-256 in the signed/tracked `ReleaseWorkflow` Python code. Runtime reads use `os.open(O_RDONLY|O_NOFOLLOW)`, exact regular-file/UID/mode/size checks, matching lstat/fstat device and inode identity, bounded descriptor reads, a second fstat after reading, and constant-time digest comparison. Both assets are fully verified and parsed before any SSH inspection or FTPS mutation. Tests reject helper and manifest tampering, symlinks, wrong mode, injected wrong owner, lstat/open replacement, and post-read descriptor identity change; they also prove that pre-verification failures make no remote call or write.

The installer now copies the tracked frozen manifest instead of regenerating and trusting a new inventory. The manifest represents only the immediately preceding immutable server runtime and deliberately excludes the new helper. Bundle validation still compares exact source and signed bundle bytes, and the signed manager performs the independent pinned checks when the assets are consumed.

## Direct CLI and concurrency integration

The direct-repository `main()` bootstrap test no longer mocks `provision_legacy_helper_assets`. It constructs the real `ReleaseWorkflow`, resolves the tracked repository helper and frozen manifest, exercises the initial SSH-read failure and exact confirmation flow, observes the real helper-only bootstrap call at the FTPS/SSH test boundaries, and verifies the second SSH config read starts the manager. A separate resolver test fixes both repository and `Contents/Resources` layouts and their expected modes.

The PHP two-writer CAS test no longer sleeps. Its test-only helper copy writes one byte to a dedicated child ready pipe immediately before attempting the production lock. The parent holds the actual lock, waits for both ready bytes, then releases it; exactly one writer must return `changed` and the other `conflict`.

## TDD and verification

RED command:

`python3 -m unittest tests.python.test_release_workflow -q`

Result before implementation: eight errors because the tracked frozen manifest, pinned constants, strict descriptor reader, and new provision interface did not exist.

Focused GREEN command:

`php tests/php/test_manage_private_config.php && python3 -m unittest tests.python.test_macos_installer tests.python.test_manager tests.python.test_release_workflow -q`

Result: PHP helper E2E passed; 151 focused Python tests passed.

Full verification:

`bash tests/run-all.sh`

Result: 376 Python tests passed, 1 existing opt-in real-Mac SSH test skipped; all PHP tests, syntax and compile checks, installer receipt verification, and public secret scan passed. `git diff --check`, Python byte-compilation, and PHP lint also passed.

## Concerns

None. Updating either bootstrap asset or deliberately defining a different legacy generation now requires an intentional review of the pinned size and digest in trusted code; routine runtime reads cannot silently redefine their own trust anchor.

---

# Final review fixes: production bootstrap RETR boundary

## Root cause and resolution

The legacy helper bootstrap required `verify_private_file_hashes` for every old fixed-runtime file after SSH had already proven the complete legacy inventory `EXACT`. On the production server those owner-only 0600 old runtime files intentionally reject FTPS RETR with 550, so this redundant verification made the initial bootstrap impossible and allowed `ftplib.error_perm` to escape.

The old-runtime FTPS hash-read block was removed. The fail-closed boundary remains:

1. signed local helper and frozen legacy manifest pass pinned descriptor validation;
2. SSH reports the full new inventory `PARTIAL`;
3. SSH independently reports the complete old inventory `EXACT`, including owner, type, mode, size, SHA-256, and absence of unexpected immutable entries;
4. FTPS atomically writes only the new helper and returns the exact readback bytes;
5. FTPS verifies the new helper's exact bytes and 0700 mode;
6. SSH reports the complete new inventory `EXACT`.

No other partial state is accepted. The existing fixed-runtime provisioning path and the helper's own FTPS verification are unchanged.

## TDD and verification

RED regression:

`python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_bundled_legacy_assets_bootstrap_when_old_ftps_retr_is_denied -v`

Result before the fix: `ftplib.error_perm: 550 RETR denied` escaped from the redundant old-file FTPS verification.

The regression now makes old-file `verify_private_file_hashes` raise the production-equivalent 550 and requires bootstrap success. The direct manager integration also asserts that the old-file hash check is never called.

Focused GREEN command:

`python3 -m unittest tests.python.test_release_workflow tests.python.test_manager -q`

Result: 100 tests passed.

Full verification:

`bash tests/run-all.sh`

Result: 376 Python tests passed, 1 existing opt-in real-Mac SSH test skipped; all PHP tests, syntax and compile checks, installer receipt verification, and public secret scan passed.

## Operational scope and concerns

No production deployment, push, or Mac app installation was performed. Concerns: none.

---

# Final review fixes: production helper write over SSH

## Root cause and design

The fixed runtime and its `bootstrap` directory are intentionally 0700. Production FTPS cannot create the helper's temporary upload in that directory and returns 550 `Operation not permitted`; therefore even helper-only FTPS upload is not a viable bootstrap mechanism.

`ReleaseWorkflow.provision_legacy_helper_assets` now uses a new trusted-SSH operation after the existing full-new `PARTIAL` and legacy `EXACT` inspections. It performs no FTPS read or write for bootstrap. The general release deployment and normal FTPS paths are unchanged.

`RemoteValidator.provision_fixed_helper` uses the existing SSH trust snapshots, verified no-follow config reads, `ssh -G` host/policy validation, fixed SSH options, bounded subprocess runner, and trust rechecks. Its remote command is one pinned `/usr/bin/php8.5 -r <fixed code>` argv. The remote root, helper relative path, expected mode/hash, and base64 helper bytes are carried only in bounded stdin JSON; helper bytes and paths are never interpolated into argv or shell code.

The pinned PHP operation requires:

- exact `/home/<account>/private/xserver-mail-lineworks` structure with dot-segment rejection;
- exact `bootstrap/manage-private-config.php` relative name, 0700 mode, bounded nonempty bytes, and matching SHA-256;
- trusted `/` and `/home`, owner-controlled non-symlink directory chain, established 0700/0701 account boundary, and exact 0700 private/runtime/bootstrap directories;
- an absent target or an already exact owner/regular/0700/size/hash target;
- an exclusive randomized temp file in `bootstrap`, bounded descriptor writes, `fflush`, chmod 0700, pre/post fstat identity, descriptor SHA-256, rename, and final lstat/UID/mode/size/inode/SHA-256 verification.

An existing nonexact target, unsafe path, symlink, wrong owner/mode, malformed payload, hash mismatch, SSH trust change, or unexpected result fails closed with fixed local errors. The final full-runtime SSH inspection must still report `EXACT` before bootstrap succeeds.

## TDD and verification

RED evidence:

- the release regression failed with `ftplib.error_perm: 550 helper temp write denied` from `replace_bytes_atomic`;
- the RemoteValidator regressions failed because `provision_fixed_helper` and `fixed_helper_php` did not exist.

Focused GREEN command:

`python3 -m unittest tests.python.test_remote_validator tests.python.test_release_workflow tests.python.test_manager -q`

Result: 133 tests passed, 1 existing opt-in SSH test skipped. Tests cover fixed argv, stdin-only payload, trust replacement, local and remote path attacks, exact idempotency, nonexact target rejection, and complete bootstrap with FTPS helper writes forced to 550/not called.

Full verification:

`bash tests/run-all.sh`

Result: 380 Python tests passed, 1 existing opt-in real-Mac SSH test skipped; all PHP tests, syntax and compile checks, installer receipt verification, and public secret scan passed.

## Operational scope and concerns

No production deployment, push, or Mac app installation was performed. Concerns: none.
