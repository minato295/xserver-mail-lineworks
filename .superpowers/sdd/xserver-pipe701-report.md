# Xserver pipe-space / 0701 wrapper implementation report

## Outcome

- The managed mail-filter target is now emitted byte-for-byte as `| /usr/bin/php8.5 <fixed-wrapper-path>`.
- The fixed private wrapper is deployed at `/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php` with exact mode `0701` and exact source bytes:

  ```php
  <?php
  require __DIR__ . '/mail-forward-command.php';
  ```

- The existing stable bootstrap remains `mail-forward-command.php` at mode `0700`; directories remain `0700`, ordinary files `0600` or `0700`, and the only additional allowed file mode is exact `0701`.
- Fixed-runtime FTPS readback and SSH manifest inspection include the wrapper, so an already provisioned exact wrapper converges as `EXACT`; absent complete trees converge through the existing staged publish flow. Partial, wrong-content, wrong-mode, symlink, and ownership mismatches fail closed.
- Initial migration recognizes both previously managed stable targets (`|/usr/bin/php8.5 .../mail-forward-command.php` and `| /usr/bin/php8.5 .../mail-forward-command.php`). It retains add/readback-before-delete ordering and durable journal recovery, and does not broaden authorization to unrelated rules.
- The wrapper source is tracked outside `public_html`; installer/resource and repository public scans cover it without embedding production identifiers.

## TDD evidence

The first focused run failed for the intended missing behavior: the command lacked the post-pipe space, the wrapper file was absent, FTPS rejected `0701`, and deploy collapsed `0701` to `0700`. A separate migration test failed because the no-space historical target was not recognized. Each was then made green with the minimum production changes.

## Verification

Command: `bash tests/run-all.sh`

Result: exit 0; 332 tests passed, 1 explicit opt-in real-Mac SSH contract test skipped. Composer metadata, PHP tests, Python tests, syntax checks, installer verification, and the public secret/path scan all passed.

`git diff --check` also completed with no errors.

## Concerns / operational notes

- `XSERVER_COMMAND_PATH` remains a required local-config field for backward compatibility, but production target construction now deliberately derives both stable and wrapper paths from `XSERVER_HOME`; its value no longer selects the managed target.
- FTPS cannot guarantee server-side `fsync`; this change preserves the existing same-directory staging, rename, readback, and recovery contract.
- The real-Mac SSH integration test remains opt-in because it requires separate credentials. The system-PHP fixed-runtime ownership, symlink, parent-mode, exact-manifest, `ABSENT`, `PARTIAL`, and `EXACT` contracts ran locally.

## Follow-up review hardening

An updated review identified that the initial FTPS allowlist admitted mode `0701` at arbitrary private paths. A second red/green cycle added a single path-bound mode policy used by upload planning, atomic upload, `assert_file_mode`, and batch verification. Exact `0701` is now accepted only at:

- `/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php`
- `/private/.fixed-staging-<32 lowercase hex>/bootstrap/mail-forward-command-701.php`

Tests prove rejection for generic private paths, alternate filenames, short generation IDs, and uppercase generation IDs. Deployment validates the complete local upload plan before opening FTPS, preventing partial mutation when a later file has a forbidden mode/path pair.

## Wrapper source provenance hardening

A final review required the local selected release tree to be treated as untrusted for the privileged wrapper body. `ReleaseWorkflow` now owns the exact canonical wrapper bytes as a constant. After the existing regular-file/non-symlink checks and before any SSH or FTPS operation, provisioning reads the selected wrapper source and requires byte equality. The staged wrapper is then written from the code-owned constant, not recopied from the mutable source path.

Regression tests replace the source with whitespace-altered, `include`-based, alternate-target, and arbitrary PHP bodies. Every variant fails with zero remote calls. The full verification command remains `bash tests/run-all.sh`.
