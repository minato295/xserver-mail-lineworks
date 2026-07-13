# Dedup Empty-State Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure an empty deduplication claim map is always persisted as a reusable JSON object.

**Architecture:** Preserve the strict object-only reader and change only the writer boundary to encode the associative claim map as an object, including when empty.

**Tech Stack:** PHP 8.5, existing PHP test runner, shell full-suite runner.

## Global Constraints

- No filters, mailbox files, webhook payloads, config schema, or operational log schema change.
- JSON arrays remain invalid dedup state on read.
- Empty state is written as `{}` with newline, mode 600, through the existing atomic write path.
- No secrets, real messages, or personal information enter tests or commits.

---

### Task 1: Canonical empty-object persistence

**Files:**
- Modify: `src/DeliveryDeduplicator.php`
- Modify: `tests/php/test_delivery.php`

**Interfaces:**
- Consumes: `DeliveryDeduplicator::reserve()`, `release()`, `commit()`, and the existing strict `readClaims()` contract.
- Produces: `writeClaims()` output that is a JSON object for both empty and non-empty maps.

- [ ] **Step 1: Write the failing test**

Reserve and release the only hash, assert the decoded file is an object and the next reserve succeeds. Also assert a manually supplied `[]` remains rejected.

- [ ] **Step 2: Verify RED**

Run: `php tests/php/test_delivery.php`

Expected: FAIL because releasing the final reservation writes `[]` and the next reserve rejects it.

- [ ] **Step 3: Implement the minimal writer fix**

In `writeClaims()`, JSON-encode `(object) $claims` with the existing flags and newline. Do not relax `readClaims()`.

- [ ] **Step 4: Verify GREEN and regression**

Run: `php tests/php/test_delivery.php && bash tests/run-all.sh`

Expected: delivery tests and full suite PASS with only the existing explicit skip; public secret scan PASS.

- [ ] **Step 5: Commit**

```bash
git add src/DeliveryDeduplicator.php tests/php/test_delivery.php
git commit -m "fix: persist empty dedup state as object"
```

### Task 2: Guarded production state repair

**Files:**
- Modify: production private dedup state only; no repository file

**Interfaces:**
- Consumes: the configured private dedup path, its sibling `.delivery-dedup.lock`, and the exact known-invalid empty-list SHA-256/size/mode/owner tuple.
- Produces: atomically published `{}\n` with mode 600 and a reusable dedup store.

- [ ] **Step 1: Acquire the production dedup lock**

Open the existing sibling lock as a regular owner-only file, verify parent directory owner/mode 700, and acquire `LOCK_EX`. Refuse symlinks, wrong owner, wrong mode, or a changed directory identity.

- [ ] **Step 2: Recheck the invalid state immediately before replacement**

Under the lock, require the exact known-invalid SHA-256, size 3, mode 600, owner, regular-file type, and bytes `[]\n`. Every other state stops without mutation.

- [ ] **Step 3: Atomically publish the valid empty object**

Write `{}\n` to one owner-only sibling temporary file, flush and fsync it, chmod 600, atomically rename it over the guarded state, fsync the directory, then read back exact bytes/mode/owner/hash before releasing the lock.

- [ ] **Step 4: Verify reuse without a webhook**

With the newly active release class, reserve one synthetic hash, confirm a second reserve is suppressed, release it, reserve it again, release it again, and confirm the final raw state is exactly `{}\n`. This must not construct or invoke `WebhookClient`.

- [ ] **Step 5: Confirm operational readiness**

Read back active release, fixed-runtime hashes/modes, permanent managed filter count, probe-filter count, and the latest operational event metadata without displaying addresses, subjects, bodies, credentials, or webhook URLs.
