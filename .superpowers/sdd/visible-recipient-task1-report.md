# Visible Recipient Auto-Sync — Task 1 Report

## Scope

Implemented Task 1 only: XServer mail-account/forwarding reads, reverse forwarding reachability, strict response validation, and the new managed filter shape.

## TDD red evidence

Command:

```text
python3 -m unittest tests.python.test_xserver_api -v
```

Observed before production changes: exit 1, 20 tests run, `FAILED (failures=1, errors=11)`. The forwarding tests raised `AttributeError` for the absent `list_mail_accounts`, `list_forwarding_addresses`, and `discover_forwarding_sources` methods. The managed-filter test failed because `header` + `contain` was not yet accepted. These were the expected feature-missing failures.

## Implementation

- Added the fixed-origin `/v1/server/{servername}/mail-account` collection URL.
- Added `list_mail_accounts(domain)` and the documented domain query.
- Added `list_forwarding_addresses(address)` with percent-encoded account path.
- Added fail-closed validation for missing, malformed, and duplicate address data, with case-folded canonical results.
- Added cycle-safe reverse reachability from a base address; only XServer account sources enter the result, so external destinations are excluded.
- Changed managed filter recognition to require exactly `header` + `contain` + a full address + the existing managed copy command.

## Green evidence

Selected command:

```text
python3 -m unittest tests.python.test_xserver_api -v
```

First green run: exit 0, 20 tests run, `OK`.

## Files

- `manager/xserver_api.py`
- `tests/python/test_xserver_api.py`
- `.superpowers/sdd/visible-recipient-task1-report.md`

## Commit

Recorded after final verification.

## Concerns

None. Task 2 and later files were not changed.

## Review fix evidence

The review tests were changed before production code to require XServer's official
`/v1/server/{servername}/mail` collection, the `accounts` response key, and
`/mail/{mail_account}/forwarding`. They also added comma-delimited and malformed
local/domain cases for API responses and managed-rule keywords.

RED command:

```text
python3 -m unittest tests.python.test_xserver_api -v
```

Observed with the previous implementation: exit 1, 20 tests run,
`FAILED (failures=4, errors=2)`. Failures covered the old collection URL/schema,
the old forwarding path, and acceptance of comma-delimited/dot-malformed values.

The client now uses the official mail collection contract and validates addresses
with the same local-part, domain-label, and length constraints as
`manager.validate_email`, without importing the manager and creating a cycle.

GREEN command:

```text
python3 -m unittest tests.python.test_xserver_api -v
```

Observed after the fix: exit 0, 20 tests run, `OK`.
