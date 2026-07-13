# Visible Recipient Auto-Sync Final Fix Report

## Result

All findings from `visible-recipient-final-review.md` are addressed.

## Changes

- Managed `header` / `contain` rules now require a canonical, exact equality between `rule.domain` and the keyword address domain. Cross-domain same-command rules remain unrelated and untouched.
- Normal menu 13 synchronization recognizes only the authorized legacy `to` / `match` / full-address / same-copy-command shape. It adds and reads back every expected new rule before retiring legacy and stale rules. The persistent scope journal supports same-address migration and fresh-process recovery after an interrupted legacy deletion.
- Configurations predating `dedup_path` derive `delivery-dedup.json` beside the validated private `log_path`. The existing deduplicator enforces a real, owner-controlled 0700 parent and 0600 state/lock files. Explicit invalid `dedup_path` values still fail closed.
- The design and tests now state the Message-ID-free identity precisely: SHA-256 of the complete raw RFC 5322 input. Only the digest is stored; content is not stored. Byte rewrites intentionally produce a distinct identity to favor collision resistance.
- README and the approved design describe legacy rule migration, crash recovery, old-config compatibility, and raw-message fallback semantics.

## TDD Evidence

The new classifier, legacy migration, crash recovery, and legacy-config tests were observed failing before their production changes, then passing afterward.

Focused verification:

```text
python3 -m unittest tests.python.test_xserver_api tests.python.test_manager tests.python.test_scope_journal -q
Ran 87 tests — OK
php tests/php/test_delivery.php — PASS
php tests/php/test_release_validator.php — PASS
git diff --check — clean
```

Full verification:

```text
./tests/run-all.sh
Ran 318 tests — OK (skipped=1 opt-in live Mac SSH contract)
PASS: public secret scan
PASS: all tests, syntax checks, and public secret scan
```

## Remaining Concerns

No code blocker remains. Production API mutation, release switching, and end-to-end mail delivery remain operator-gated operational steps and were not performed by this local final-fix pass.
