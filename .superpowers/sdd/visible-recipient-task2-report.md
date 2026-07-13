# Task 2 implementation report

Status: `DONE_WITH_CONCERNS`

## Scope implemented

- Added live forwarding-closure discovery through `MailManager.expected_targets()`.
- Added fail-closed reconciliation planning through `plan_target_sync()`.
- Added Japanese dry-run additions/deletions to diagnostics without API writes.
- Added menu item 13 and exact confirmation phrase `通知対象を同期する`.
- Added managed `header` + `contain` rule construction.
- Added additions-first synchronization with full API readback before stale deletion.
- Preserved unrelated filters through canonical full-snapshot checks.
- Added remote-config compare-and-swap checks before confirmation, after confirmation,
  and after API reconciliation; `notification_targets` is uploaded only after API success
  and is then read back.
- Extended `ScopeJournal` with validated pending-authorization readback and tests proving
  interrupted add/delete state cannot authorize an unknown body or rule.

## RED evidence

Command:

```text
python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v
```

Result before implementation: 60 tests ran, 6 errors. Every new manager test failed
with the expected missing-interface errors for `expected_targets`, `plan_target_sync`,
or `sync_targets`. The two journal schema tests passed because the existing journal
validation already rejected unknown resumptions.

A later focused race test was also observed RED:

```text
python3 -m unittest tests.python.test_manager.ManagerTest.test_target_sync_remote_config_race_after_api_reconciliation_never_uploads -v
```

It failed because the config race was detected only after an upload attempt. The
implementation then added the required pre-upload compare-and-swap read.

## GREEN evidence

Selected command:

```text
python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v
```

Fresh result: 61 tests ran, 61 passed, 0 failures/errors.

Broader command (before the final additional race guard, whose selected test passed):

```text
python3 -m unittest discover -s tests/python -v
```

Result: 310 tests ran, 309 passed, 1 explicitly skipped, 0 failures/errors.

## Files

- `manager/manage.py`
- `manager/scope_journal.py`
- `tests/python/test_manager.py`
- `tests/python/test_scope_journal.py`

Implementation commit: `ce91a5d` (`feat: safely sync visible notification recipients`)

## Concerns

- Menu 13 uses `_ScopedMigrationApi`'s strict per-process authorization state, while
  `ScopeJournal` now validates durable interrupted-operation records independently.
  The new menu action does not yet persist its `_ScopedMigrationApi` state to the FTPS
  journal across a process restart. The scoped API still fails closed on unknown rules,
  and all requested add/readback/delete/config ordering is covered, but durable automatic
  continuation of a menu-13 interruption would require wiring the journal store into that
  action in a follow-up.
- Tasks 3 and 4 were not implemented.

## Important-review fixes

Status: `DONE`

- Wired menu 13 to an FTPS-backed `ScopeJournal` at the existing private
  `deploy-transactions/target-sync-scope.json` root. The journal is written and
  read back with mode 0600 before each API mutation, records pending add/delete
  authorization, resumes only the matching body hash or rule ID after restart,
  and is committed only after API and private-config readback succeed.
- Added real interruption/restart tests using a shared fake FTPS store. One test
  interrupts after an accepted add and proves a fresh manager adopts the exact
  rule without adding twice; another interrupts after an accepted delete and
  proves a fresh manager records completion without deleting twice.
- Removed menu items 2-4 and their dispatch entries. The legacy methods also fail
  closed if called directly, so auto-sync is the only notification-target writer;
  menu item 1 remains available for listing.

### Fix RED evidence

The new restart tests initially failed because
`deploy-transactions/target-sync-scope.json` was never created. After wiring the
journal, the interrupted-add test additionally caught an attempted duplicate add;
the recovery baseline was corrected to adopt only the pending authorized hash.
The removed-menu test initially entered the legacy add flow and failed on its
prompt input, proving the bypass was still reachable.

### Fix GREEN evidence

```text
python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v
```

Fresh result: 61 tests ran, 61 passed, 0 failures/errors.

### Remaining concerns

- Tasks 3 and 4 remain outside Task 2 review-fix scope.

## Config-commit crash recovery fix

- Added two process-restart regressions covering interruption immediately after
  the private-config upload and interruption after successful config readback but
  before the journal commit write.
- When an active journal is found with remote targets already equal to the live
  expected set, recovery now verifies that the API has no remaining diff, the
  journal has no pending operation, every unrelated rule retains its exact full
  digest, every journaled new rule has the authorized body digest, and every old
  ID is both retired and absent. Only then is the journal marked committed; no API
  or config write is replayed.

### Crash-recovery RED/GREEN evidence

Both new tests initially errored with `同期journalの追加対象が一致しません`, reproducing
the administrative wedge from an empty recomputed plan. After the recovery branch
was added, both focused tests passed without duplicate API additions.

Fresh selected verification:

```text
python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v
```

Result: 63 tests ran, 63 passed, 0 failures/errors.

## Exact surviving-ID recovery fix

- Completed-state recovery now requires the current API ID set to equal exactly
  the journal-authorized surviving IDs (`unrelated` plus `new_ids`). Retired old
  IDs and every unknown extra ID are rejected before journal commit.
- Added a restart regression that interrupts after config upload, injects an
  unrelated API rule, and verifies recovery raises without API mutation and
  leaves the journal active.

The regression was observed RED because recovery returned successfully and
committed the journal. After adding the exact-set check, the focused regression
and the selected suite passed.

```text
python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v
```

Fresh result: 64 tests ran, 64 passed, 0 failures/errors.
