# FTPS directory mode hardening report

## Result

- `_ensure_dirs` now sends `SITE CHMOD 700 <path>` for every absolute-path component after each `mkd` attempt, including when `mkd` reports that the directory already exists.
- `deploy_release` therefore applies mode 700 to the remote root and every parent of nested release files.
- Private config and log parent creation uses the same behavior; the former redundant final log-directory CHMOD was removed.
- File upload modes are unchanged: executable local files use 700, other files (including the PHP entry point) use 600.
- CHMOD failures remain uncaught and abort deployment.
- Existing private-path validation is unchanged.

## TDD evidence

RED command:

`python3 -m unittest tests.python.test_ftps_deployer.FtpsDeployerTest.test_ensure_dirs_chmods_existing_directories_to_700 tests.python.test_ftps_deployer.FtpsDeployerTest.test_deploy_chmods_every_nested_release_directory_to_700`

Result before implementation: 2 tests run, 2 failures. Both failures showed the expected missing directory CHMOD commands.

GREEN command: same focused command.

Result after implementation: 2 tests run, OK.

## Fresh verification

Command:

`python3 -m unittest discover -s tests/python -v && php -l bin/mail-to-lineworks.php && php -d assert.exception=1 tests/php/test_delivery.php && git diff --check`

Result:

- Python: 93 tests run, OK.
- PHP syntax: no syntax errors.
- PHP delivery/fallback: PASS.
- `git diff --check`: no output, exit 0.

## Scope and concerns

- Changed tracked files: `manager/ftps_deployer.py`, `tests/python/test_ftps_deployer.py`.
- Unrelated changes in `docs/superpowers/plans/2026-07-11-macos-launcher-app.md` were preserved and excluded from this commit.
- No known concerns.

## Review follow-up

- Raw `.` path segments are now rejected before `PurePosixPath` normalization for both remote private paths and `filesystem_home`. Absolute paths with repeated separators remain accepted, while relative paths, `..`, and case-insensitive `public_html` segments remain rejected.
- `deploy_release` now ensures and applies mode 700 to `remote_root` and every parent immediately after connecting, including for an empty local release.
- A regression test confirms that a directory `SITE CHMOD` failure propagates before any `STOR` or rename.

Follow-up RED command:

`python3 -m unittest tests.python.test_ftps_deployer.FtpsDeployerTest.test_private_paths_reject_raw_dot_segments_before_normalization tests.python.test_ftps_deployer.FtpsDeployerTest.test_private_path_validation_preserves_absolute_root_and_empty_segments tests.python.test_ftps_deployer.FtpsDeployerTest.test_private_paths_still_reject_parent_and_public_html_segments tests.python.test_ftps_deployer.FtpsDeployerTest.test_empty_release_chmods_remote_root_and_all_parents_to_700 tests.python.test_ftps_deployer.FtpsDeployerTest.test_directory_chmod_failure_aborts_before_store_or_rename`

Result before implementation: 5 tests run with 3 expected failures (both raw-dot subtests and the empty-release directory provisioning assertion). The CHMOD ordering regression already passed against the existing propagation behavior.

Follow-up focused GREEN: 7 tests run, OK (the five review tests plus `filesystem_home` and nested-release coverage).

Follow-up full verification:

`tests/run-all.sh && git diff --check`

Result: 102 Python tests run, OK; PHP/Composer checks passed; public secret scan passed; `git diff --check` exited 0 with no output.
