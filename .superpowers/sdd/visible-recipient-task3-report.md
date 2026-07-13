# Visible Recipient Auto-Sync — Task 3 Report

## Status

DONE

Task 3 only was implemented. Task 4 documentation, packaging, deployment, and production synchronization were not changed.

## TDD evidence

### RED

1. After adding the initial Task 3 tests, `php tests/php/test_delivery.php && php tests/php/test_release_validator.php` exited 255 because `NotifierConfig::$dedupPath` did not exist.
2. After adding the Message-ID-less fallback test, `php tests/php/test_delivery.php` exited 255 with `Message-ID fallback must deduplicate identical metadata without merging distinct messages`.
3. After adding the empty-object state test, `php tests/php/test_delivery.php` exited 255 with `Invalid deduplication state` while reading `{}`.
4. After adding the private-directory test, `php tests/php/test_delivery.php` exited 255 with `Non-private dedup directory must be rejected`.

Each failure was observed before its corresponding production implementation.

### GREEN

Fresh required verification:

```text
$ php tests/php/test_delivery.php && php tests/php/test_release_validator.php
PASS: delivery and fallback
release validator tests passed
```

Focused checks also passed:

- `php -l` for `DeliveryDeduplicator.php`, `NotifierConfig.php`, `DeliveryApplication.php`, and `mail-to-lineworks.php`
- `git diff --check`

## Files

- Created `src/DeliveryDeduplicator.php`
- Modified `src/NotifierConfig.php`
- Modified `src/DeliveryApplication.php`
- Modified `bin/mail-to-lineworks.php`
- Modified `config/config.example.json`
- Modified `tests/php/test_delivery.php`
- Modified `tests/php/test_release_validator.php`

## Implemented behavior

- 600-second SHA-256 claim TTL with sibling `flock` lock file
- bounded JSON object validation, expiry pruning, atomic sibling-temp replacement, and mode 0600
- absolute/private/non-symlink path validation and 0700 state-directory enforcement
- application-level suppression before webhook delivery
- normalized fallback hash for messages without Message-ID
- fail-open delivery on store failure with an allowlisted, secret-free operational event
- required `dedup_path` configuration and entrypoint construction

## Concerns

None for Task 3. Production configuration must provide a private absolute `dedup_path` whose existing parent directory is mode 0700; deployment/config migration belongs to Task 4.
# Review-finding fixes

Status: `DONE`

- Message-ID-less messages now use SHA-256 of the complete raw RFC 5322 input, so body and attachment-byte changes cannot collide through parsed metadata; only SHA-256 identifiers are persisted.
- Replaced pre-delivery claims with opaque reserve/commit/release leases. Reservations suppress concurrent duplicates, successful webhooks commit, and webhook/reporter failures release. Abandoned reservations expire after the bounded TTL.
- Canonicalized the state directory and use one fixed same-directory lock identity. Directory, lock, state, and temporary files are checked through open descriptors against path device/inode identity, owner, type, mode, and link count.
- Atomic state publication now flushes and `fsync()`s the temporary file before rename and `fsync()`s the containing directory afterward. Platforms without PHP `fsync()` fail closed in the dedup store while delivery remains fail-open.

### TDD and verification

- RED: the focused PHP test first failed on missing `reserve()`, proving the lease regression was active.
- GREEN: `php tests/php/test_delivery.php && php tests/php/test_release_validator.php` passed.
- PHP lint passed for `DeliveryDeduplicator.php`, `DeliveryApplication.php`, and `test_delivery.php`; `git diff --check` passed.
- `./tests/run-all.sh` ran all 314 Python tests successfully (1 skipped) and all PHP checks, then failed only its pre-existing public-secret scan on `tests/python/test_xserver_api.py` containing `outside@external.invalid`, outside this Task 3 diff.

### Concerns

- None in the Task 3 PHP scope. The unrelated repository secret-scan fixture failure remains for its owning task.
