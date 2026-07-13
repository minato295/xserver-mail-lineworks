# Task 1 implementation report

## Result

DONE_WITH_CONCERNS

## Changes

- Added shared Python and PHP canonical email validators. Local-part case is preserved; domains are lowercased; lists are byte-sorted and deduplicated, with strict persisted readback support.
- Routed manager input, XServer API readback, ongoing error-recipient changes, and the Mac application bundle through the shared Python validator.
- Added pre-release private-config migration with CAS/readback, one-time 32-byte HMAC-key generation, unknown-key preservation, canonical pinned/target/error arrays, and aggregate recipient/path bounds.
- Added strict PHP persisted-config validation, decoded 32-byte `systemMailHmacKey`, and derived `delivery-health.json` path.
- Added fixed `health-summary` SSH/helper operation with redacted exact output, bounded file reads, owner/mode/link checks, duplicate-key rejection, timestamp/sequence/hash/classification validation, and parent-chain validation.
- Updated the candidate helper size/hash pin in `manager/release_workflow.py`. This was a plan dependency omission approved by the parent agent and is the only production-file expansion beyond the original Task 1 file list.

## RED evidence

- `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api -v`
  - RED: `ModuleNotFoundError: manager.email_address`; the shared canonical validator did not exist.
- `php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php`
  - RED: `NotifierConfig::$systemMailHmacKey` was undefined and the decoded-key assertion failed.
- `python3 -m unittest tests.python.test_private_config_ssh.PrivateConfigSshTest.test_health_summary_uses_fixed_operation_and_exact_redacted_schema -v`
  - RED: `PrivateConfigSsh.health_summary()` did not exist.
- `php tests/php/test_manage_private_config.php`
  - RED: fixed `health-summary` operation was rejected; after the basic operation existed, an ancestor-symlink fixture was incorrectly accepted.
- `python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_pinned_legacy_asset_constants_match_tracked_assets -v`
  - RED: pinned candidate helper size/hash did not match the changed helper bytes.

## GREEN evidence

- `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api tests.python.test_macos_installer tests.python.test_private_config_ssh -v`
  - GREEN: 170 tests.
- `php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php`
  - GREEN: both scripts report PASS.
- `php -l src/CanonicalEmail.php && php -l src/NotifierConfig.php`
  - GREEN: both files report no syntax errors.
- `python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_pinned_legacy_asset_constants_match_tracked_assets -v`
  - GREEN: 1 test.
- `git diff --check`
  - GREEN: no whitespace errors.

## Commit

- `e12ce84 feat: validate private notification configuration`

## Concerns

- This intermediate commit must not be deployed to production before Task 3 is complete. Task 3 owns old/new helper dual-prefix migration, backup, generation manifest, and the migration branch; Task 1 intentionally did not implement those pieces.
- `manager/release_workflow.py` was pulled forward only to keep the signed candidate-helper pin consistent. No Task 3 migration behavior was added.

## Independent review follow-up

Commits:

- `131cc81 fix: harden private health configuration`
- `cb38fa7 fix: tokenize health state keys safely`

### Important findings resolved

- Added one identical aggregate-size model to manager, private helper, and `NotifierConfig`: 32 recipients, 900 To bytes, 4096 UTF-8 log-path bytes, longest unfolded header at most 997 bytes, and complete signed message at most 65535 bytes. The fixed non-configurable bounds are 160 header bytes and 8192 message bytes; configurable To/log bytes are added explicitly.
- Added reachable 32/33, To 900/901, and log 4096/4097 fixtures across Python manager, PHP helper, and `NotifierConfig`. Invalid manager fixtures prove zero config CAS, key generation, API writes, stage calls, and locator switches. Every accepted fixture proves header `< 998` and signed message `< 65536`.
- Replaced raw-key duplicate detection with decoded JSON-key token comparison and added an escaped `\\u0073tate`/`state` duplicate regression.
- Made `PrivateConfigSsh.health_summary()` match the helper's exact missing/healthy/degraded schemas. Healthy null/null and all tested list/dict/bool field variants now fail as one fixed redacted `RuntimeError` without exception chaining.
- Retained every health parent snapshot and revalidates the chain after descriptor read. The opened descriptor and post-read descriptor/path are checked for regular-file type, 0600 mode, owner, nlink, dev, inode, and stable size. Deterministic parent-swap and file-swap race tests fail closed.
- Added pre-release CAS conflict/bad-readback tests proving stage/API mutation zero, plus add/change/delete recipient tests proving canonical persistence and byte-for-byte preservation of the HMAC key and unrelated values.

### Additional RED evidence

- `python3 -m unittest tests.python.test_private_config_ssh.PrivateConfigSshTest.test_health_summary_rejects_inexact_or_unsafe_responses -v`
  - RED: list-valued `state` raised an unredacted `TypeError` during set membership; healthy null/null was accepted.
- `php tests/php/test_manage_private_config.php`
  - RED: escaped-equivalent duplicate keys were accepted; a parent directory swapped to a symlink after health-file read was also accepted.
- `python3 -m unittest tests.python.test_manager.ManagerTest.test_pre_release_config_bounds_stop_before_mutation -v`
  - RED: the fixed worst-case formula interface was absent (`MailManager._runtime_config_sizes` missing).
- `php tests/php/test_delivery.php`
  - RED: `NotifierConfig` did not expose/prove the computed worst-case header and signed-message byte counts.
- `python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_pinned_legacy_asset_constants_match_tracked_assets -v`
  - RED: the changed helper bytes no longer matched the signed size/hash pin.

### Fresh GREEN evidence

- `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api tests.python.test_macos_installer tests.python.test_private_config_ssh -v`
  - GREEN: 174 tests.
- `php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php`
  - GREEN: both scripts report PASS, including boundary and deterministic race fixtures.
- `php -l src/CanonicalEmail.php && php -l src/NotifierConfig.php`
  - GREEN: both files report no syntax errors.
- `python3 -m unittest tests.python.test_release_workflow -v`
  - GREEN: 21 tests, including candidate-helper pin verification.
- `git diff --check`
  - GREEN: no whitespace errors.

### Remaining concern

- The Task 1 changes still do not add Task 2 behavior. Task 3-owned migration behavior was not changed by this follow-up; only the candidate helper size/hash pin was refreshed.

## Independent re-review follow-up

Commit: `484522d fix: close missing health state races`

### Findings resolved

- The missing-health branch now revalidates all retained parent snapshots, confirms the file is still missing on the same chain, and revalidates that chain again before returning `missing`.
- Added a deterministic pre-`lstat` ancestor rename/symlink replacement. The replacement has no health file; the helper now returns nonzero with empty stdout and the one fixed stderr line.
- Added every explicitly required health invalid fixture: ordinary and escaped-equivalent duplicate keys, unknown/missing keys, 4097-byte file, wrong mode, simulated wrong-owner comparison, hardlink, direct symlink, invalid timestamp, reversed sequence order, invalid message hash, and non-allowlisted classification. Every fixture asserts the exact same redacted failure response.
- Added add/change/delete action-path coverage for both CAS conflict and bad readback. All six cases leave manager/client persisted state unchanged and prove zero API, stage, and locator mutation.

### RED evidence

- `php tests/php/test_manage_private_config.php`
  - RED: after parent snapshots, replacing the missing-health ancestor with a symlink still returned a successful `missing` summary; the exact fixed-failure assertion failed.
- `python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_pinned_legacy_asset_constants_match_tracked_assets -v`
  - RED: helper size changed from 24415 to 24631 bytes and no longer matched the signed candidate pin.

### Fresh GREEN evidence

- `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api tests.python.test_macos_installer tests.python.test_private_config_ssh`
  - GREEN: 175 tests.
- `php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php`
  - GREEN: both scripts report PASS; helper PASS includes the complete fixed-error invalid matrix and missing/existing race barriers.
- `php -l src/CanonicalEmail.php && php -l src/NotifierConfig.php`
  - GREEN: both files report no syntax errors.
- `python3 -m unittest tests.python.test_release_workflow`
  - GREEN: 21 tests, including the refreshed helper pin.
- `git diff --check`
  - GREEN: no whitespace errors.

### Concern

- Wrong-owner behavior is exercised deterministically by a test-only hook limited to the generated helper copy because the unprivileged test process cannot create a file owned by another UID. Production still compares both path and opened-descriptor UID to `posix_geteuid()`.

## Final test-contract follow-up

Commit: `1c8eb4a test: assert fixed health failures exactly`

- Corrected the prior report's over-broad statement that every invalid health fixture already asserted the exact fixed response. Four pre-existing cases only checked a nonzero code at that point.
- Escaped-equivalent duplicate keys, unsafe parent-chain symlink, post-read parent replacement, and post-read file replacement now all use `checkFixedHealthFailure()` and therefore require: nonzero exit, empty stdout, and exact stderr `private config operation failed\n`.
- This is assertion-only hardening. No new behavioral RED was feasible because production already emitted the fixed response; the previous assertions simply did not prove it. Running the strengthened helper suite directly produced GREEN.

Fresh verification:

- `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api tests.python.test_macos_installer tests.python.test_private_config_ssh` — 175 tests, GREEN.
- `php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php` — both PASS with the strengthened exact assertions.
- `php -l src/CanonicalEmail.php && php -l src/NotifierConfig.php` — no syntax errors.
- `python3 -m unittest tests.python.test_release_workflow` — 21 tests, GREEN; helper pin remains exact because production helper bytes did not change.
- `git diff --check` — GREEN.
