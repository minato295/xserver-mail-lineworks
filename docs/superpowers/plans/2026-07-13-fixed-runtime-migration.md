# Fixed Runtime Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the three fixed Xserver runtime files from the exact legacy generation to the reviewed stdin-frame generation without exposing a nonrecoverable mixed state.

**Architecture:** Add dual-protocol selection to the verified stable bootstrap. Extend `ReleaseWorkflow.provision_fixed_runtime()` with one narrowly scoped, generation-pinned, resumable migration that creates a verified private backup before advancing through three atomic file replacements.

**Tech Stack:** PHP 8.5 CLI, Python 3 standard library, existing FTPS/SSH validators, unittest, existing PHP test runner.

## Global Constraints

- Permanent Xserver filters, mailbox files, private config, dedup state, logs, and `public_html` are never mutated by the migration.
- Only the exact signed legacy generation or one allowed old/new prefix state may advance.
- Backup directories are 700 and files are 600/700; all bytes are hash/readback verified before live replacement.
- Order is `ReleaseValidator.php` → `validate-release.php` → dual stable bootstrap; unknown or indeterminate state fails closed.
- Frame is selected only from the fully verified active release manifest record for `src/StdinFrame.php`; no decode failure downgrades to legacy.

---

### Task 1: Dual bootstrap and resumable fixed migration

**Files:**
- Modify: `bin/stable-mail-entrypoint.php`
- Modify: `manager/release_workflow.py`
- Modify: `tests/php/test_stable_bootstrap.php`
- Modify: `tests/python/test_release_workflow.py`
- Modify: `docs/superpowers/specs/2026-07-13-xserver-php-stdin-frame-design.md`

**Interfaces:**
- Produces: verified active-release capability selection in the standalone bootstrap.
- Produces: `ReleaseWorkflow` migration from one pinned legacy full-tree manifest through exactly three prefix states.
- Consumes: existing `FtpsDeployer.read_bytes()`, `replace_bytes_atomic()`, `deploy_release()`, `publish_directory()`, `verify_private_file_hashes()` and `RemoteValidator.inspect_fixed_runtime()`.

- [ ] **Step 1: Write failing tests**

Add PHP fixtures for old manifest→legacy config FD and new manifest containing verified `src/StdinFrame.php`→stdin frame. Add Python fakes that model legacy, each prefix, new exact, unknown combination, replacement-result ambiguity, backup partial/exact, and injected abort after each atomic replacement. Assert rerun reaches new exact, backup exists first, order is exact, and unrelated remote paths/API calls stay untouched.

- [ ] **Step 2: Verify RED**

Run: `php tests/php/test_stable_bootstrap.php && python3 -m unittest tests.python.test_release_workflow -v`

Expected: FAIL because the bootstrap is frame-only and the workflow rejects the legacy fixed tree rather than migrating it.

- [ ] **Step 3: Implement the minimal dual bootstrap**

Set `$frameCapable = isset($seenPaths['src/StdinFrame.php'])` only after every runtime record has passed path/mode/size/hash validation. Use the current frame path when true. When false, use the former config-FD branch for the exact verified legacy release. Never catch a frame-path failure and retry legacy.

- [ ] **Step 4: Implement resumable migration**

Load and independently pin the legacy manifest. Add the current helper record, construct the four allowed full-tree manifests, and detect exactly one current prefix through SSH exact inspection. At prefix zero, build and publish a private generation backup of the three verified old bytes before replacement. At every prefix, require the backup exact, verify current hash, atomically replace one file, resolve uncertain results by readback, and require the next whole-tree exact. Preserve the backup for rollback.

- [ ] **Step 5: Verify GREEN and full regression**

Run: `php tests/php/test_stable_bootstrap.php && python3 -m unittest tests.python.test_release_workflow -v && bash tests/run-all.sh`

Expected: focused tests and full suite PASS; public secret scan PASS.

- [ ] **Step 6: Commit**

```bash
git add bin/stable-mail-entrypoint.php manager/release_workflow.py tests/php/test_stable_bootstrap.php tests/python/test_release_workflow.py docs/superpowers/specs/2026-07-13-xserver-php-stdin-frame-design.md docs/superpowers/specs/2026-07-13-fixed-runtime-migration-design.md docs/superpowers/plans/2026-07-13-fixed-runtime-migration.md
git commit -m "fix: migrate fixed runtime to stdin framing"
```
