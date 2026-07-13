# Fixed runtime upgrade fix report

## Outcome

`fixed_runtime_php` now treats only these hardcoded top-level directories as mutable:

- `releases`
- `state`
- `deploy-transactions`
- `logs`

Their contents are not traversed or compared with the immutable runtime manifest. Each mutable root itself must still be a real, non-symlink directory owned by the effective SSH user with exact mode `0700`. Unknown top-level entries remain fatal. The SSH payload has no field or API argument that can extend the allowlist.

The immutable `bootstrap`, `src`, and `vendor` entries continue through the existing exact type, mode, size, and SHA-256 comparison.

## Root cause

The inspector recursively materialized every entry below `/private/xserver-mail-lineworks` and compared that complete map with the fixed-runtime manifest. After a successful deployment, legitimate operational trees were therefore indistinguishable from corrupt extra fixed-runtime entries, so a later `ReleaseWorkflow.provision_fixed_runtime` stopped before staging.

## TDD evidence

RED command:

```text
python3 -m unittest -v tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_fixed_runtime_ignores_only_safe_mutable_top_level_trees tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_fixed_runtime_rejects_unsafe_mutable_roots
```

Before the production change, the exact fixed tree plus all four legitimate mutable roots failed with PHP exit 255. The unsafe-root test already passed, confirming the regression test failed for the upgrade bug rather than fixture setup.

GREEN focused verification:

```text
python3 -m unittest -v tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_fixed_runtime_contract_on_temporary_tree tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_fixed_runtime_ignores_only_safe_mutable_top_level_trees tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_fixed_runtime_rejects_unsafe_mutable_roots tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_accepts_xserver_0701_account_home_boundary tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_rejects_unsafe_account_home_and_loose_private_suffix_modes tests.python.test_remote_validator.RemoteValidatorTest.test_real_system_php_rejects_writable_trust_and_symlinked_or_loose_suffix
```

Result: 6 tests passed.

Focused component verification:

```text
python3 -m unittest -v tests.python.test_remote_validator tests.python.test_release_workflow
```

Result: 33 tests passed, 1 opt-in live SSH contract skipped.

Full verification:

```text
bash tests/run-all.sh
```

Result: 320 tests passed, 1 opt-in live SSH contract skipped; Composer metadata, PHP syntax/tests, Python compilation, and public secret scan passed.

## Security properties covered

- Mutable contents, including symlinks, are intentionally opaque to the fixed-runtime comparison.
- A mutable root with a loose mode, symlink type, or regular-file type is rejected.
- An unknown top-level entry is rejected even if the caller supplies a fabricated `ignored_top_level` payload field.
- Existing parent trust, ownership, mode, and symlink checks remain unchanged.
- Existing fixed file hashes, sizes, types, and modes remain exact.

## Remaining concern

The live Xserver SSH contract test remains opt-in and was not run because it requires separate production-like credentials. The behavior is exercised against the real local system PHP implementation.
