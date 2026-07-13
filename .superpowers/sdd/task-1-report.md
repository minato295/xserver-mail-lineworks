# Task 1 Report: Dual bootstrap and resumable fixed migration

## Status

DONE

## TDD RED evidence

Tests were written before production changes.

Command:

`php tests/php/test_stable_bootstrap.php; python3 -m unittest tests.python.test_release_workflow -v`

Observed PHP failure:

`FAIL: verified legacy release did not use config FD`

Reason: the stable bootstrap unconditionally emitted the stdin frame, so the verified legacy fixture could not receive configuration through its legacy descriptor.

Observed Python result:

19 tests ran; the seven new migration executions errored at the existing `ReleaseWorkflowError: 固定runtimeが部分状態のため停止しました。`

Reason: the existing workflow rejected exact legacy and allowed prefix trees as generic partial fixed-runtime states. Existing unrelated tests remained green.

## Implementation

- The standalone bootstrap derives `$frameCapable` only after every active release runtime record passes path, mode, size, hash, PHP-opening, descriptor-identity, and readback validation.
- A verified manifest containing `src/StdinFrame.php` uses the stdin frame. A verified manifest without that record uses the legacy config FD. Frame-path errors never retry legacy.
- `ReleaseWorkflow` independently reads the code-pinned legacy manifest, adds only the pinned `manage-private-config.php` record, and proves that the current generation differs in exactly the three reviewed files.
- It constructs exactly four whole-tree manifests: legacy and the three ordered prefixes (`ReleaseValidator.php`, `validate-release.php`, stable bootstrap).
- Prefix zero reads the three hash/mode-verified legacy files, builds an owner-only generation staging backup outside `public_html`, verifies FTPS hashes and SSH whole-tree exactness, atomically publishes it, and verifies the permanent backup again before live replacement.
- Every transition verifies the old live hash immediately before atomic replacement, resolves an uncertain result through bounded readback, and requires the next whole-tree prefix to be exact.
- Prefixes one and two require the permanent backup already to be exact. Unknown combinations, partial backups, extra changes, bad modes, and missing assets fail closed.
- Existing `logs`, `state`/dedup, `releases`, filters, mailbox, private config, API state, and `public_html` are not mutated.

## Coverage

- PHP: verified old manifest selects config FD; verified new manifest record selects frame; no caller environment legacy values survive the frame path.
- Python: exact legacy, every allowed prefix, exact ordered replacements, permanent backup-before-live ordering, backup bytes/modes/hashes, exception-after-rename ambiguity, process abort after each replacement and resume, non-prefix rejection, partial-backup rejection, and unrelated remote path/API invariance.
- Mechanical comparison of tracked `fixed-runtime/legacy-manifest.json` plus the pinned helper against the current generated fixed tree showed no key difference and exactly these changed records: `src/ReleaseValidator.php`, `bootstrap/validate-release.php`, `bootstrap/mail-forward-command.php`.

## GREEN and verification evidence

Focused command:

`php tests/php/test_stable_bootstrap.php && python3 -m unittest tests.python.test_release_workflow -v`

Observed: PHP PASS; 19/19 Python tests PASS.

Full command:

`php tests/php/test_stable_bootstrap.php && python3 -m unittest tests.python.test_release_workflow -v && bash tests/run-all.sh`

Observed: focused PASS; full suite 386 tests PASS, 1 explicit real-Mac SSH test skipped; PHP tests, syntax checks, compile checks, and public secret scan PASS.

## Self-review and concerns

- Migration is deliberately one-way and supports only the exact pinned legacy generation plus its three forward prefixes; rollback continues to use the preserved backup and the documented reverse order.
- A failed replacement that leaves the old bytes does not blindly retry within the same call. It fails closed; a later explicit rerun reclassifies the whole-tree prefix.
- The durable generation backup is intentionally retained after success.
- No production Xserver E2E was run in this task; the design still requires a new Message-ID through the permanent filter and an HTTP 200 operational event after deployment.

## Review fix: revalidate backup before every prefix

An Important review finding identified that the first implementation verified the complete permanent backup only once before entering the prefix loop. A different backup file or directory could therefore change after the first live replacement without stopping the second replacement.

### Fix RED

Command:

`python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_migration_revalidates_complete_backup_before_each_live_replacement -v`

Observed expected failures:

- `other-file`: `AssertionError: 1 != 2` because a different corrupted backup file was noticed only after the second live replacement.
- `directory`: `AssertionError: ReleaseWorkflowError not raised` because a corrupted backup directory was never inspected again.

### Fix implementation

Immediately before every live prefix transition, the workflow now:

1. requires `RemoteValidator.inspect_fixed_runtime(backup_filesystem, backup_entries)` to return `EXACT`, binding directories, file types, modes, sizes, hashes, ownership, symlink absence, and absence of extras;
2. calls `verify_private_file_hashes()` for all three permanent backup files, rebinding every file's mode, size, and SHA-256 through FTPS readback;
3. only then verifies the current live old hash and performs the next atomic replacement.

Either failure raises `ReleaseWorkflowError` before another live mutation.

### Fix GREEN and regression

Focused command:

`python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_migration_revalidates_complete_backup_before_each_live_replacement -v && php tests/php/test_stable_bootstrap.php && python3 -m unittest tests.python.test_release_workflow -v`

Observed: new file/directory tamper regression PASS; PHP PASS; 20/20 release-workflow tests PASS.

Full command:

`bash tests/run-all.sh`

Observed: 387 tests PASS, 1 explicit real-Mac SSH test skipped; all PHP, syntax, compile, and public secret scan checks PASS.
