# FTPS batch verification report

## Root cause

`ReleaseWorkflow.provision_fixed_runtime()` verified every fixed-runtime file with
one `read_optional_bytes()`/`read_bytes()` call and one `assert_file_mode()` call.
Each call opened, authenticated, protected, and closed a separate `FTP_TLS`
session. A runtime with N files therefore required 2N FTPS sessions per
verification pass, which exposed production to connection throttling and long
hangs.

## Change

- Added `FtpsDeployer.verify_private_files(expected, allow_all_missing=False)`.
- The method validates every private path and `(expected_bytes, exact_mode)`
  tuple before connecting.
- One connection performs every bounded `RETR` and exact `MLST` check, with
  passive mode and `PROT P` established by the existing protected connection
  path.
- Downloads retain at most `len(expected_bytes) + 1` bytes and fail on oversized
  or differing content.
- MLST accepts exactly one file entry, rejects duplicate facts, requires exactly
  one canonical `unix.mode` or `perm-mode` fact, and accepts only exact `0600` or
  `0700` values.
- FTP 550 returns `False` only when every path is missing and
  `allow_all_missing=True`. Partial presence fails closed. Other FTP errors are
  replaced by fixed, secret-free errors.
- `ReleaseWorkflow.provision_fixed_runtime()` now batches the initial fixed-tree
  check, staging verification, and final verification. SSH preflight, staging,
  and post-publish inspections remain in place, including SSH/FTPS state
  comparison and fail-closed mismatch handling.

## TDD evidence

The deployer tests first failed with `AttributeError: 'FtpsDeployer' object has
no attribute 'verify_private_files'`. After the batch implementation, the four
new deployer regression tests passed.

The workflow test then failed because the legacy per-file
`read_optional_bytes()` path was still called. After migrating the workflow, it
passed while explicitly forbidding all three legacy per-file verification
methods. A separate MLST `type=dir` regression was observed failing before the
parser was tightened and passing afterward.

Coverage includes one connection/login/`PROT P`/quit for multiple files,
oversize, all-missing, partial-missing, content mismatch, mode mismatch,
ambiguous MLST, non-file MLST, invalid inputs, new-tree staging, existing-tree
verification, and preservation of SSH/FTPS cross-protocol checks.

## Verification

- `python3 -m unittest tests.python.test_ftps_deployer tests.python.test_release_workflow -v`
  — 46 tests passed.
- `git diff --check` — passed.
- `bash tests/run-all.sh` — 325 tests passed, 1 explicit opt-in SSH contract
  test skipped; PHP checks, Python checks, and public secret scan passed.

## Concerns

No known correctness or secret-exposure concerns remain. FTPS still performs
one `RETR` and one `MLST` command per file, as required for byte and exact-mode
verification, but connection/authentication overhead is now constant per batch.

## P1 review resolution: bind MLST facts to the requested path

Review found that the initial parser retained the MLST pathname only while
tokenizing the response and then discarded it. A correct mode fact for another
file could therefore be accepted after downloading the requested file.

The parser now receives the requested absolute remote path, retains each parsed
entry as `(pathname, facts)`, and accepts only one canonical file entry whose
pathname exactly equals the argument sent to `MLST`. It rejects a different
absolute path, basename-only output, multiple distinct entries, and duplicate
entries, even when every reported mode is correct.

TDD evidence: the new pathname-binding regression initially failed for both the
different-path and basename-only cases. After binding the parsed entry, the
focused FTPS/workflow suite passed 47 tests, including mismatch, ambiguous, and
multiple-line cases.
