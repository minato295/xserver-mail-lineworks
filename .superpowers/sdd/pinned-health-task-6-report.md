# Pinned health Task 6 implementation report

## Result

DONE

Commit: `f734e42a124a827ebbfa72da51978c1013ad2a2b docs: package and operate recovery notifications`

No deployment, server mutation, Mac-app installation, network request, real sendmail, GitHub push, or sanitized-history construction was performed.

## RED evidence

The required test-first command was:

`python3 -m unittest tests.python.test_release_workflow tests.python.test_manager -v && php tests/php/test_release_validator.php && php tests/php/test_validate_release_entrypoint.php`

The first run exited 1 but did not meet the requested RED gate because the new Python packaging assertion had been attached to the suite's deliberately minimal synthetic source, which did not contain the eight runtime files. No production code had been changed. The fixture was corrected by copying the exact tracked runtime files into the synthetic release source.

The corrected RED run used the same exact command and exited 1 after 131 Python tests. All eight-file packaging assertions passed. The only failures were the new diagnostic expectations: 5 failures and 1 error for absent `pinned`, `targets`, and `health` result fields and absent Japanese `恒久対象`, `通知対象`, `未作成`, `正常`, `障害中`, and `状態ファイル不正` output.

A later privacy self-review added a focused regression for the legacy private release-path output:

`python3 -m unittest tests.python.test_manager.ManagerTest.test_default_diagnostics_compare_api_rules_with_remote_config_and_release tests.python.test_manager.ManagerTest.test_diagnostics_report_pinned_and_target_drift_without_private_values -v`

It exited 1 with 2 expected failures because `/private/releases/r1` and the synthetic private release ID were still rendered. Production diagnostics were then changed to output only `設定済み` or `不明`.

## GREEN evidence

Final focused command:

`python3 -m unittest tests.python.test_release_workflow tests.python.test_manager -v && php tests/php/test_release_validator.php && php tests/php/test_validate_release_entrypoint.php`

Result: exit 0; 131 Python tests passed; `release validator tests passed`; `validate-release entrypoint tests passed`.

Final full gate:

`bash tests/run-all.sh`

Result: exit 0. All Task 4/5 PHP suites, existing PHP suites, PHP syntax checks, Python compilation, Mac completion-receipt verification, and the public secret scan passed. Python reported `Ran 425 tests` and `OK (skipped=1)`; the only skip was the pre-existing explicit real-Mac SSH opt-in fixture. The final lines were `PASS: public secret scan` and `PASS: all tests, syntax checks, and public secret scan`.

`git diff --check` and `git diff --cached --check` both exited 0 before commit.

## Changes

- `manager/manage.py`: Japanese count-only pinned/API/config diagnostics; allowlisted missing/healthy/degraded health rendering; one fixed invalid-state message; no address diff or private release path output.
- `README.md`: private pinned-list workflow, journal/recovery order, outage/recovery semantics, at-least-once crash duplicate boundary, manager-generated HMAC key handling, and privacy-safe health-state diagnostics.
- `config/config.example.json`: only `operator@example.invalid` and `base@example.invalid`, with pinned and full readback target arrays; no HMAC key value.
- `tests/python/test_release_workflow.py`: exact manifest presence, mode 0600, size, and tracked SHA-256 coverage for all eight runtime files.
- `tests/php/test_release_validator.php`: all eight runtime files plus per-file omission, tamper, and wrong-mode rejection.
- `tests/php/test_validate_release_entrypoint.php`: exact packaged hash/mode checks for all eight runtime files.
- `tests/python/test_manager.py`: pinned drift, target drift, missing/healthy/degraded/invalid health, and address/key/hash/token/path secrecy coverage.
- `tests/run-all.sh`: Task 4/5 system-mail, sendmail, and health suites; literal `system_mail_hmac_key` value detection that does not flag the field name itself.

`manager/release_workflow.py` required no production change: its generic plain-tree and stable-manifest builders already include and exactly hash all PHP runtime sources. `macos/install_app.py` required no production change: its exact resource sets already include every Python module introduced by Tasks 1–5. The new tests explicitly verify these existing generic guarantees.

## Self-review

- Confirmed all eight named runtime files are in the generated stable manifest with mode 0600 and exact tracked hashes.
- Confirmed validator failure coverage for every named file's omission, tampered hash, and wrong mode.
- Confirmed diagnostics never render addresses, HMAC keys, Message-ID hashes, webhook tokens, exception contents, state paths, or private release paths.
- Confirmed public address examples are limited to `operator@example.invalid` and `base@example.invalid` and no sample HMAC key is published.
- Confirmed the secret scan recognizes only a quoted 43-character value assigned to `system_mail_hmac_key`; the field name alone is allowed.
- Confirmed no real HTTP, sendmail, deployment, server, app-install, push, or production migration operation ran.
- Confirmed only the eight Task 6 files were committed. The pre-existing dirty `.superpowers/sdd/task-2-report.md`, `task-3-report.md`, and `task-4-report.md` remained unstaged.

## Concerns

None for Task 6 implementation. Independent task/whole-branch review and all production rollout steps remain for the parent workflow and were not performed here.

## Whole-branch review fix wave

### Result and design choice

DONE. All four whole-branch findings were fixed in one wave.

Journal I/O now crosses the existing verified `PrivateConfigSsh` boundary through only two operations and an exact two-value journal-kind enum (`target-sync` or `filter`). The PHP helper derives both fixed paths under `$home/private/xserver-mail-lineworks/deploy-transactions`; callers cannot supply a path. Journal bytes remain confined to the authenticated SSH stdin/stdout exchange and are never printed or included in errors. The helper proves the account/private parent chain, exact 0700 descendants, regular/no-symlink/effective-owner/nlink=1/mode-0600 files, descriptor/path identity, stable size/metadata, and pre/post atomic-replacement identity. Missing, symlink, hardlink, wrong owner/type/mode, unsafe parent owner/mode, and deterministic parent/file replacement races fail with one fixed redacted error.

Exact post-CAS recovery now adopts a complete config only when both intended arrays and the journal's complete semantic `config_expected_sha256` match. It durably records the observed raw digest as `config_applied_sha256`, then continues without another API/config mutation. Partial arrays, divergent config, unrelated external changes, API drift, and mismatched durable evidence remain rejected.

`ScopeJournal` now rejects decoded duplicate object keys, including ordinary and escaped-equivalent spellings. `PrivateConfigSsh` uses the exact helper/runtime health classification set: `success`, `invalid_payload`, `invalid_parameter`, `missing_parameter`, `invalid_webhook_url`, `rate_limited`, `http_error`, `transport_error`, `forced_test_failure`, `internal_error`, `system_mail_suppressed`, `health_state_failure`, and `unknown`.

### RED evidence

- `python3 -m unittest tests.python.test_manager.ManagerTest.test_target_sync_restart_adopts_exact_post_cas_config_without_extra_mutation -v` — 1 error at the old permanent `CAS証跡` rejection.
- `python3 -m unittest tests.python.test_scope_journal.ScopeJournalTest.test_rejects_ordinary_and_escaped_equivalent_duplicate_json_keys -v` — 1 test, 2 expected failures because ordinary and escaped-equivalent duplicates were accepted.
- `python3 -m unittest tests.python.test_private_config_ssh.PrivateConfigSshTest.test_health_summary_allowlist_exactly_matches_runtime_and_helper tests.python.test_private_config_ssh.PrivateConfigSshTest.test_scope_journal_uses_fixed_operations_and_validates_responses -v` — 2 tests, 6 errors: five valid runtime classifications were rejected and the fixed journal methods were absent.
- `php tests/php/test_manage_private_config.php` — exited 255 at the test's exact missing journal-trust hook/function assertion before production helper edits.

### GREEN and final evidence

- The four exact targeted commands above passed after implementation; the PHP helper cases cover fixed path/kind allowlisting, atomic mode-0600 readback, symlink, hardlink, wrong owner/type/mode, unsafe parent owner/mode, and deterministic parent/file replacement races.
- `python3 -m unittest tests.python.test_manager tests.python.test_scope_journal tests.python.test_private_config_ssh tests.python.test_ftps_deployer tests.python.test_remote_validator -v && php tests/php/test_manage_private_config.php` — 205 Python tests, OK (1 pre-existing opt-in skip), then PHP PASS.
- `php -l bin/manage-private-config.php` — no syntax errors. `python3 -m py_compile ...` and `git diff --check` — exit 0.
- `bash tests/run-all.sh` — exit 0; 428 Python tests, OK (1 pre-existing real-Mac SSH opt-in skip), all PHP suites/syntax checks, completion-receipt verification, and public secret scan passed. Final line: `PASS: all tests, syntax checks, and public secret scan`.

No deployment, server access/mutation, Mac app installation, network request, real sendmail, real secret/address use, GitHub push, or sanitized-history construction was performed. Fixtures remain deterministic and use only `example.invalid` addresses. The pre-existing dirty `task-2-report.md`, `task-3-report.md`, and `task-4-report.md` were preserved and will not be staged.

## Second-wave whole-branch re-review fixes

### Result

DONE. The three re-review findings were fixed without deployment, network, Mac-app, real-sendmail, push, or production-data operations.

Journal writes now retain the initially verified prior file identity and parent trust through baseline comparison, rename, parent-directory durability, and exact readback. The temporary descriptor is flushed and fsynced before rename; a descriptor for the originally verified mode-0700 parent is identity-checked, fsynced after rename, and checked again before exact readback. Same-owner file or parent replacement is rejected after initial read, before rename, and before readback for both `target-sync` and `filter`. File-fsync failure preserves the prior journal; directory-fsync failure returns the fixed redacted error while leaving exact renamed bytes resumable.

The 32-recipient and 900-byte comma-only signed-To bounds now use only canonical `error_recipients` in the manager, fixed helper, and `NotifierConfig`. Canonical validation of pinned/full notification arrays and all log/header/message limits remain unchanged. The fixed helper's code-owned size/SHA metadata was updated to the reviewed bytes.

`SystemMailAuthenticator::build()` now only normalizes CRLF and lone CR to LF for hash/HMAC construction and preserves zero, one, or multiple terminal LFs on the wire. `DeliveryHealthMonitor` already supplied one terminal LF; its transition vectors now prove exactly one terminal CRLF.

### RED evidence

- `php tests/php/test_manage_private_config.php` exited 255 at `test-only journal after-read write barrier must be exact`, because the writer had no retained verified-prior identity seam. The completed test also injects file/directory fsync failures and file/parent replacement at all three write boundaries for both journal kinds.
- `python3 -m unittest tests.python.test_manager.ManagerTest.test_pre_release_config_bounds_stop_before_mutation tests.python.test_manager.ManagerTest.test_pre_release_bounds_do_not_count_notification_targets_as_error_recipients tests.python.test_manager.ManagerTest.test_pinned_target_bounds_ignore_notification_targets_when_error_recipients_fit -v` exited 1: the comma-only 900-byte valid case and target-heavy configs were rejected.
- `php tests/php/test_delivery.php` exited 255 at `to-900 must be accepted`.
- `php tests/php/test_system_mail.php && php tests/php/test_health_monitor.php` exited 255 because `build()` collapsed multiple terminal LFs to one.

One test indentation error and one test-only missing mock name were corrected and rerun before production edits; the rerun failures above were behavioral.

### GREEN and full evidence

- The exact three-manager focused command passed 3 tests.
- `php tests/php/test_manage_private_config.php` passed all helper cases, including both journal kinds, both fsync failures, and 12 deterministic replacement cases (file and parent at three boundaries for two kinds).
- `php tests/php/test_delivery.php && php tests/php/test_system_mail.php && php tests/php/test_health_monitor.php` passed.
- A first journal GREEN exposed a replacement after the pre-rename check; a second retained-identity check immediately before rename closed it. A broader Python run then exposed stale fixed-helper pinned metadata; after updating exact size/SHA, the isolated bootstrap test and all 29 release-workflow tests passed.
- `bash tests/run-all.sh` exited 0 with all PHP suites, PHP syntax, Python compilation, Mac completion-receipt verification, and public secret scan passing. A concise fresh Python run reported `Ran 429 tests` and `OK (skipped=1)`; the skip remains the explicit real-Mac SSH opt-in fixture.

## Third-wave whole-branch review fixes

### Protocol

The remaining pathname replacement race is removed without shell commands, FFI, or a caller-supplied path. Each fixed journal kind has exactly three allowlisted recovery artifacts: a transaction staging file, a published transaction marker, and an exact next-body file. The marker records the fixed kind, intended bytes/hash, and the prior journal's existence, descriptor identity, and hash. It is published with a no-overwrite hard link only after its descriptor is mode/owner/link/size checked and fsynced; the verified parent descriptor is fsynced at each publication/cleanup boundary.

For an existing journal, the helper retains the verified prior `r+b` descriptor and the verified parent descriptor. Immediately after final path/descriptor/parent/next-artifact checks and the deterministic final barrier, it rewrites only that retained descriptor with truncate/write/flush/fsync/readback. It never renames or unlinks the journal pathname, so a same-owner interposed valid journal is preserved and the postcheck fails redacted. For a missing journal, the exact fsynced next-body inode is linked to the still-missing fixed pathname with atomic no-overwrite creation after the equivalent final barrier. The retained next descriptor is checked both before and after the mutation; interposed target, parent, or artifact identities are never discarded.

Restart strictly inventories the dedicated directory, validates every recognized artifact as regular/no-symlink/effective-owner/mode `0600` with the exact permitted link count and retained parent identity, validates the marker's exact duplicate-free schema and hashes, and completes or cleans only a recognized transaction state. A partial same-inode existing write is resumed from the marker; a completed missing hard link is finalized; a different pathname identity is preserved and rejected. Unknown, malformed, wrong-mode, symlink, hardlink, and directory artifacts fail with the fixed redacted response.

The two manager remediation strings now explicitly say `エラー通知先` for the 32-recipient and 900-byte limits.

### RED evidence

- `python3 -m unittest tests.python.test_manager.ManagerTest.test_pre_release_config_bounds_stop_before_mutation -v` ran one test with two failing subtests: `count-33` and `to-901` expected `エラー通知先...` but production emitted generic `通知先...`.
- `php tests/php/test_manage_private_config.php` exited 255 at the exact source-contract assertion `test-only final existing-journal mutation barrier must be exact`; the old pathname-`rename()` implementation had no required post-final-check/pre-mutation existing or missing seam.
- After the helper bytes changed, `python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_pinned_legacy_asset_constants_match_tracked_assets -v` failed `34612 != 50707`, proving the signed candidate pin was stale before it was refreshed.

### Focused GREEN and crash evidence

- `php tests/php/test_manage_private_config.php` reports PASS. For both `target-sync` and `filter`, it covers existing and missing final target interposition, final parent replacement, retained-next replacement, exact preservation of interposed bytes, and restart after the conflict is removed.
- For both kinds and both existing/missing prior states, injected failures after durable marker publication, durable next-body publication, existing descriptor mutation or missing no-overwrite link, and immediately before artifact cleanup all return the one fixed redacted error; the next process resumes to exact intended bytes. File-fsync and parent-fsync failures are also restarted.
- The artifact matrix rejects unknown names plus corrupt, wrong-mode, symlink, hardlink, and directory transaction artifacts. Ordinary journal trust tests continue to reject unsafe type/owner/mode/link/parent identities and file/parent read races.
- `python3 -m unittest tests.python.test_manager tests.python.test_scope_journal tests.python.test_private_config_ssh tests.python.test_release_workflow -v && php tests/php/test_manage_private_config.php && php -l bin/manage-private-config.php` exited 0: 154 Python tests passed, the helper suite passed, and PHP syntax was valid.
- Candidate helper metadata is pinned to the reviewed 51,225 bytes and SHA-256 `ba23260373bf175a0906c16be8ff9a325e4c74a59e83fee3a5d01400bfea3026`.

### Full verification

- `bash tests/run-all.sh` exited 0. All PHP suites and syntax checks passed; Python reported `Ran 429 tests` and `OK (skipped=1)`, with only the existing explicit real-Mac SSH opt-in skipped. The completion-receipt verification and public secret scan passed. Final line: `PASS: all tests, syntax checks, and public secret scan`.
- No deployment, server/network operation, Mac-app installation, real sendmail, production identifier/secret use, push, or history rewrite was performed.

## Fourth-wave persistent-slot redesign

### Rationale and protocol

The fourth review showed that the stage/marker/next cleanup protocol could not make
pathname publication and deletion identity-bound across every crash boundary. It was
therefore removed rather than incrementally patched. Each fixed journal kind now owns
one persistent mode-0600 lock plus two persistent mode-0600 slots. The slot inodes are
created once with `x+b`, fsynced with the verified parent directory, retained while the
per-kind exclusive lock is held, and never renamed or unlinked by the protocol.

Every canonical slot record binds the fixed journal kind, its parity-bound generation,
both slot device/inode identities, prior target existence/device/inode/hash, intended
body/hash, and a checksum over the canonical record fields. A pair is authoritative
only when both records validate and their generations are consecutive; the greater
generation is selected deterministically. Slot replacement and target repair use only
retained descriptors, with file fsync, parent-directory fsync, identity revalidation,
and exact descriptor readback. Existing targets are rewritten through their retained
descriptor; missing targets use no-overwrite `x+b` creation.

Bootstrap adoption is deliberately narrower than ordinary recovery. Before any durable
pair can have existed, slot 0 may be zero or an exact strict prefix while slot 1 is
still zero, or a complete generation-0 slot 0 may authorize only a zero/exact-prefix
generation-1 slot 1. A valid generation-1 slot never repairs an invalid generation-0
slot. Arbitrary remnants, checksum drift, cross-kind/cross-inode substitution,
nonconsecutive generations, and all post-generation invalid slots fail closed without
deletion. A checksum-valid pair may repair only an identity-authorized prior/intended
target or its exact crash prefix.

### RED evidence

- The inherited RED `php tests/php/test_manage_private_config.php` exited 255 at
  `persistent journal recovery must have no pathname cleanup phase`, proving the old
  cleanup protocol was still present before production replacement.
- After the production helper changed, the first full `bash tests/run-all.sh` exited 1.
  The helper suite itself passed, while Python reported 9 failures and 25 errors, all
  rooted in the deliberately stale signed helper size/SHA-256 pin. This proved the new
  helper could not enter the fixed-runtime migration path until its exact reviewed pin
  was refreshed.
- During GREEN, focused crash tests separately exposed and fixed: valid inactive slots
  being rejected on ordinary generation advance; an undecodable slot-0 strict-prefix
  bootstrap remnant not resuming; and a missing-origin target prefix not resuming after
  `x+b` creation.

### GREEN and final verification

- `php tests/php/test_manage_private_config.php` exited 0. The rewritten matrix covers
  both fixed kinds, deterministic generations `0/1` then `2/1`, canonical record
  checksums, retained slot inodes/modes, all three first-write partial descriptor
  points, post-generation invalid-slot rejection, valid-pair target-prefix repair,
  durable-pair zero-slot rejection, cross-kind/cross-inode substitution, arbitrary and
  valid-JSON foreign remnants, checksum-valid nonconsecutive generations, checksum
  tampering, wrong-mode/hardlinked slots, wrong-mode locks, preserved legacy artifacts,
  retained-target replacement, parent replacement, and fixed wrong-owner/unsafe-parent
  failures.
- `python3 -m unittest tests.python.test_release_workflow tests.python.test_manager -v`
  exited 0 with 132 tests after refreshing the exact helper pin.
- Final exact tracked helper metadata is 53,381 bytes and SHA-256
  `ed2defbd90a1c57b27d2c5060eab1e789c259eda462a825a1c7067d461ee3f68`.
- The final fresh `bash tests/run-all.sh` exited 0. All PHP suites and syntax checks
  passed; Python reported `Ran 429 tests` and `OK (skipped=1)`, with only the existing
  explicit real-Mac SSH opt-in skipped. Mac completion-receipt verification and the
  public secret scan passed; the final line was
  `PASS: all tests, syntax checks, and public secret scan`.
- `git diff --check`, helper/test PHP syntax, and exact helper size/SHA readback all
  passed before the scoped commit.

No deployment, server/network operation, Mac-app installation, real sendmail,
production identifier/secret use, push, PR, or history rewrite was performed. The
pre-existing dirty `task-2-report.md`, `task-3-report.md`, and `task-4-report.md` remain
unstaged and unchanged by this wave.

## Fifth-wave lock leases and durable completion evidence

The persistent journal records are now schema version 3 and bind the retained lock,
both retained slots, and the precreated retained target by device/inode. A missing
target is created once with `x+b`, mode `0600`, file fsync, parent fsync, and readback
before bootstrap publication. Generations 0 and 1 are explicitly `bootstrap`; after
the intended target bytes are durable and verified, generation 2 is published as
`complete`. Later writes likewise publish one bootstrap generation, mutate only the
bound target descriptor, and publish the following complete generation.

The existing lock artifact also carries a canonical checksummed completion high-water
mark bound to its own device/inode. It distinguishes a genuine crash during completion
publication from later corruption of an operation that already returned. Zero or exact
completion-record prefixes are adopted only below that durable high-water; after a
returned completion, active-slot zeroing/corruption fails closed. Lock, slot, target,
directory, and parent-chain identities are revalidated immediately before and after
every slot/target/high-water mutation.

The health filesystem now retains and exposes the active named-lock lease. Delivery
transitions assert it immediately before sendmail, and atomic state replacement asserts
it immediately before and after rename. A nonblocking exclusive lock on the already
retained private-directory descriptor is the non-replaceable serialization authority,
so a second instance cannot enter its callback through a replacement lock pathname;
the original instance then rejects its replaced named-lock identity.

RED evidence included the first-return generation assertion failing against the old
0/1 intent-only pair, followed by the active-slot-zero test demonstrating the exact
completion-publication ambiguity. The real two-helper journal race initially blocked
on the unrelated outer config lock; the final test-only harness bypasses only that
outer lock for process B and verifies the replacement journal lock path directly.

Focused verification passes for `php tests/php/test_manage_private_config.php` and
`php tests/php/test_health_monitor.php`. The journal matrix now separately covers
cross-kind and cross-inode substitution, equal/nonconsecutive generations, active
completion corruption, a real replacement-lock second helper, and missing-origin
zero/prefix target interposition. The health matrix uses two filesystem instances and
proves neither replacement-lock side reaches sendmail or state commit.

Final tracked helper metadata is 62,811 bytes and SHA-256
`f19fca0466c7d9bcf584b0bc15b1ffd3904d72954de13cf4f10a058c9a8394a8`.

## Sixth-wave final-review crash closure

The sixth independent whole-branch review found two pre-complete recovery gaps. A
strict prefix left while publishing the lock high-water record was rejected even
though the retained schema-3 slots independently selected the corresponding bound
`complete` generation. Also, a crash after the missing journal target was precreated
and fsynced, but before either slot was published, left an empty physical target that
the read operation rejected instead of reporting as logically missing.

Both were fixed test-first. High-water prefix repair is now authorized only after the
retained lock/slot/target identities select the matching complete generation. Repair
rewrites only the retained lock descriptor, fsyncs and reads it back, fsyncs the
retained parent descriptor, and revalidates the full lease. Empty target bytes with
both slots empty now return the fixed `missing` response. The first subsequent write
keeps the precreated target inode bound in every record while recording
`prior_target_exists=false`.

RED evidence came from `php tests/php/test_manage_private_config.php`: first at
`target-sync bound complete-slot/high-water prefix 5 must resume`, then independently
at `target-sync empty target with empty slots must read as logical missing`. After the
helper changed, the signed-pin test failed `62811 != 63969` before metadata refresh.
The completed matrix covers partial complete-slot call 4, partial high-water call 5,
and post-`x+b`/pre-slot restart for both `target-sync` and `filter`.

Focused GREEN passed for the exact signed-pin unittest, helper PHP syntax, the private
config helper suite, and the health-monitor suite. The fresh full
`bash tests/run-all.sh` gate exited 0: all PHP suites and syntax checks passed, Python
reported `Ran 429 tests` and `OK (skipped=1)` with only the existing explicit real-Mac
SSH opt-in skipped, and the completion-receipt and public-secret checks passed. No
deployment, server/network request, Mac-app installation, real sendmail, production
identifier/secret use, push, or history rewrite was performed.

Final tracked helper metadata is 63,969 bytes and SHA-256
`33e095d89ace6de5afdbdb97ad366a15cdbe198118c2196cd90d2e59e597f246`.
