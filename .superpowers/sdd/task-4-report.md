# Task 4 Report: Integration and packaging handoff

## Status

Complete for the authorized local scope. The generated Mac app now packages the SSH private-config client required by menu 14/15 as an import-only `0600` Python module. No push, main integration, installed-app replacement, Xserver mutation, production Webhook mutation, or real email was performed.

## TDD evidence

- RED: `test_bundle_contains_exact_runtime_package_and_generated_files` failed because `Contents/Resources/manager/private_config_ssh.py` was absent from `EXPECTED_BUNDLE_FILES`.
- GREEN: added the helper to the trusted source mapping and exact bundle allowlist. Reviewer follow-up corrected the import-only module mode to `0600`; the server-side PHP helper remains `0700`.
- The strengthened packaging test also checks that bundled `manage.py` contains menu dispatch for 14/15 and that helper bytes exactly match the trusted source.

## Existing generic coverage retained without duplication

- `tests/python/test_release_workflow.py` already verifies that `bin/manage-private-config.php` is included in the fixed runtime, matches source bytes and SHA-256, and has mode `0700` in FTPS verification and SSH manifest data. No vacuous duplicate assertion was added.
- `macos/install_app.py::validate_bundle` already rejects Webhook-shaped tokens and other sensitive bundle data without disclosure.
- `tests/run-all.sh` already scans tracked public source. A supplemental local scan covered tracked and untracked files for Webhook-shaped tokens and independently built and validated a generated app artifact.

## Documentation

`README.md` already documents menu 14/15, masked display, explicit reveal, exact HTTP 200 update precondition, SSH helper/CAS behavior, rollback/readback behavior, and placeholder-only examples. It contains no real deployment values, so no documentation change was needed.

## Verification

- Focused RED run: 1 expected failure for missing helper bundle entry.
- Focused GREEN run: 1/1 PASS.
- Packaging suites: `python3 -m unittest tests.python.test_macos_installer tests.python.test_release_workflow -v` — 57/57 PASS.
- Full suite: `./tests/run-all.sh` — PHP suites PASS; 359 Python tests PASS; one documented opt-in real Mac SSH test skipped; syntax checks and tracked public secret scan PASS.
- Supplemental scan: tracked/untracked/generated app Webhook secret scan PASS; generated app installer secret validation PASS.
- `git diff --check` — PASS.

## Self-review

Reviewed the feature range from base `a4ee7347756680b9d6813b7661eef6c001e1feb1` through HEAD plus the Task 4 worktree diff. Critical: 0. Important: 0 after review of the SSH fixed-command boundary, stdin-only secret transport, exact response schemas, CAS conflict/conditional restoration, secret-free failure output, quote false-positive fixtures, title normalization/encoding, and attachment display order.

## Reviewer fixes

- Important 1 RED: running generated-bundle `manager/manage.py` from outside `Contents/Resources` with a minimal environment reached the generic unexpected-error boundary instead of the expected missing-environment boundary.
- Important 1 GREEN: `manager/private_config_ssh.py` now follows the existing dual import pattern, using `manager.remote_validator` in package mode and `remote_validator` in bundled direct-execution mode. The generated-bundle subprocess reaches the Japanese `環境設定が不足しています` boundary without `ModuleNotFoundError`.
- Important 2 RED: a known legacy exact app layout, equal to the current file set minus only `manager/private_config_ssh.py`, could not be updated.
- Important 2 GREEN: `_validate_existing_destination(destination, uid)` now selects only the current exact file layout or that one frozen legacy exact layout before creating the pinned manifest. The accepted layouts are not caller-injectable. New/staged/installed bundle verification remains current-exact through `EXPECTED_BUNDLE_FILES`.
- Guard coverage rejects an extra file, any different missing file, and a mismatched bundle identifier. The real `install_with_config` path updates a legacy exact app to the current exact bundle.
- Minor RED/GREEN: generated Mac `private_config_ssh.py` changed from executable `0700` to import-only `0600`. Fixed-runtime `manage-private-config.php` coverage continues to require `0700`.
- Reviewer focused suite: 67 tests PASS across installer, release workflow, and private-config SSH tests.
- Reviewer full suite: 362 Python tests PASS, one documented opt-in SSH test skipped; all PHP, syntax, and public secret scan checks PASS.

## Handoff / concerns

The controller must separately decide and authorize all publishing and production work: push/main integration, reinstalling the generated Mac app, completion-receipt verification, Xserver release deployment, production permission audit, Webhook acceptance, and synthetic email acceptance. Those actions were intentionally not performed here.

No source-level secret or real environment value was added.
