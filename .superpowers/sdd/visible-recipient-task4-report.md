# Visible Recipient Auto-Sync Task 4 — Local Release Report

## Status

Local portion complete. No production access, deployment, Xserver API mutation, real app installation, push, or email sending was performed.

## Local changes

- Added installer/package assertions for the updated `manager/manage.py` and `manager/xserver_api.py` resources.
- Added README operations guidance for visible To/Cc matching, Bcc exclusion, forwarding-source discovery, dry-run diagnostics, the exact `通知対象を同期する` confirmation, and rollback/recovery behavior.
- Added `notification_base_address` to `config/config.example.json` and asserted that `dedup_path` remains absolute and outside `public_html`.
- Repaired the forwarding test fixture from `outside@external.invalid` to `outside@example.invalid`. It remains semantically external because that address is absent from the mocked local-account collection.

## TDD evidence

### RED

Command:

```sh
python3 -m unittest tests.python.test_macos_installer.InstallerTests.test_forwarding_aware_sync_release_resources_and_documentation -v
```

Observed: exit 1 with an assertion failure because `notification_base_address` was absent from `config/config.example.json`.

The first full-suite run also correctly rejected `outside@external.example.invalid` in the public secret scan, demonstrating that the scanner requires the exact reserved domain `example.invalid`.

### GREEN

The focused documentation/package contract passed after the minimal README and example-config updates.

The external-forwarding discovery test passed after changing the non-local mocked destination to `outside@example.invalid`:

```text
Ran 1 test in 0.000s
OK
```

The complete installer suite passed:

```text
Ran 48 tests in 4.430s
OK
```

### Full suite

Command:

```sh
./tests/run-all.sh
```

Final result:

```text
Ran 315 tests in 10.254s
OK (skipped=1)
PASS: public secret scan
PASS: all tests, syntax checks, and public secret scan
```

The single skip is the existing explicit opt-in real-Mac SSH contract test; it requires separate credentials and was intentionally not enabled for this local-only task.

## Remaining live steps (not performed)

The following Task 4 plan steps still require separately authorized live execution:

1. Stage and remotely validate a production release, including manifest, PHP config, permissions, and `public_html` checks.
2. Switch the validated release and run the target synchronization with the exact confirmation phrase, followed by API and remote-config readback.
3. Send and verify the four end-to-end To, display-name To, Cc, and Bcc messages.
4. Run final publication scans, push, verify GitHub HEAD, and reinstall the verified Mac app.

## Review follow-up: literal Xserver matching semantics

The Important finding in `visible-recipient-task4-review.md` was verified against `MailManager._rule()`: the manager configures Xserver with `field=header`, `match_type=contain`, and the literal complete notification address. It does not locally parse To/Cc or normalize display-name addresses for this routing decision.

TDD RED: the focused installer documentation contract failed because README lacked the literal `field`/`header` and `match_type`/`contain` contract. The test now also rejects the prior unsupported parsing and normalization phrases.

TDD GREEN: README now documents Xserver's whole-header substring condition, its typical To/Cc effect, the absence of a field-specific guarantee, and the fact that a Bcc address absent from delivered headers will not match. The focused contract passed (`Ran 1 test`, `OK`).
