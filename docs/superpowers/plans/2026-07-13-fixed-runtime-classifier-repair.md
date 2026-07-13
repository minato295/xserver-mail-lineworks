# Fixed Runtime Classifier Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the reviewed fixed-runtime migration recognize the exact pinned legacy/prefix production states while retaining the strict system-PHP inspector contract.

**Architecture:** Keep `fixed_runtime_php()` unchanged. Catch its expected non-match only at the migration boundary, classify all four pinned prefix manifests, and authorize mutation only when exactly one is `EXACT`; recheck that full prefix immediately before each live replacement.

**Tech Stack:** Python 3 standard library, PHP 8.5 system inspector, `unittest`, existing FTPS/SSH validators.

## Global Constraints

- Permanent filters, mailbox files, private config, dedup state, logs, and `public_html` are never mutated.
- The system-PHP inspector keeps rejecting same-path metadata/content differences, unsafe paths, symlinks, and unknown entries.
- Only one exact signed prefix among the four permitted states may authorize migration.
- Every live replacement requires a fresh exact whole-prefix check plus the existing backup and per-file checks.
- No credentials, webhook URLs, email addresses, subjects, or bodies may enter tests, logs, commits, or diagnostics.

---

### Task 1: Strict inspector-aware migration classification

**Files:**
- Modify: `manager/release_workflow.py`
- Modify: `tests/python/test_release_workflow.py`
- Test: `tests/python/test_remote_validator.py`

**Interfaces:**
- Consumes: `RemoteValidator.inspect_fixed_runtime(root, entries, *, expected_hosts)` returning `EXACT`, `PARTIAL`, or `ABSENT`, or raising `RemoteValidationError` for a strict mismatch/failure.
- Produces: migration classification in which a candidate exception means only “candidate not exact”; exactly one `EXACT` candidate is required.

- [ ] **Step 1: Write failing production-contract tests**

Make the workflow fake reproduce the real inspector by raising `RemoteValidationError` for same-shape mismatched entries. Add tests proving legacy prefix 0 and resumable prefixes 1/2 migrate, while zero exact candidates perform no backup, upload, delete, or replacement. Add a test that tampers with a different live file after one replacement and proves the next replacement is blocked by the fresh whole-prefix check.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.python.test_release_workflow -v`

Expected: FAIL because `provision_fixed_runtime()` propagates the initial strict mismatch and because no fresh whole-prefix check exists immediately before every replacement.

- [ ] **Step 3: Implement the minimal classifier boundary**

Import `RemoteValidationError`. In the initial new-manifest inspection, treat that exception as a migration candidate state only. In `_migrate_fixed_runtime()`, inspect each pinned prefix through a helper that maps `RemoteValidationError` to non-match, collect exact candidates, and require exactly one. Before each live replacement, require the current prefix inspection to return `EXACT`; otherwise stop before mutation.

- [ ] **Step 4: Verify GREEN and regression**

Run: `python3 -m unittest tests.python.test_release_workflow tests.python.test_remote_validator -v && bash tests/run-all.sh`

Expected: focused tests PASS; full suite PASS with only the existing explicit skip; public secret scan PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/release_workflow.py tests/python/test_release_workflow.py docs/superpowers/specs/2026-07-13-fixed-runtime-migration-design.md docs/superpowers/plans/2026-07-13-fixed-runtime-classifier-repair.md
git commit -m "fix: classify pinned fixed runtime prefixes"
```

### Task 2: Visible failure before reporter initialization

**Files:**
- Modify: `bin/mail-to-lineworks.php`
- Modify: `tests/php/test_delivery.php`

**Interfaces:**
- Consumes: exact `MAIL_NOTIFIER_STDIN_FRAME=1` framing contract and existing reporter initialization boundary.
- Produces: silent nonzero exit for any startup failure before `$reporter` exists; preserves the existing post-initialization reporting and delivery-safe exit behavior.

- [ ] **Step 1: Write the failing tests**

Run the no-argument entrypoint with the exact frame flag and both a malformed frame and a framed invalid configuration. Assert nonzero exit and empty stdout/stderr with no secret token. Keep an initialized delivery failure assertion at exit zero.

- [ ] **Step 2: Verify RED**

Run: `php tests/php/test_delivery.php`

Expected: FAIL because no-argument startup failures currently exit zero.

- [ ] **Step 3: Implement the minimal exit policy**

In the outer catch, set exit 1 whenever `$reporter` has not been initialized. When it exists, attempt reporting and retain exit 0 for ordinary delivery; check modes remain nonzero on every failure. Do not print the exception or input.

- [ ] **Step 4: Verify GREEN and full regression**

Run: `php tests/php/test_delivery.php && bash tests/run-all.sh`

Expected: delivery tests PASS; full suite PASS with only the existing explicit skip; public secret scan PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/mail-to-lineworks.php tests/php/test_delivery.php
git commit -m "fix: surface notifier startup failures"
```
