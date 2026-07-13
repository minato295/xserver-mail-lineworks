# Pinned Targets and LINE WORKS Health Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep private pinned notification targets across synchronization and emit authenticated, once-per-transition error and recovery email for LINE WORKS outages.

**Architecture:** The Python manager computes the union of auto-discovered and private pinned targets and commits API/config changes through a resumable version 2 scope journal. The PHP runtime authenticates its own system mail with a private HMAC key and tracks webhook observations in a strict private state machine with monotonic sequences. Existing mailbox copy delivery, deduplication, public-html exclusion, and secret-free logging remain unchanged.

**Tech Stack:** Python 3.13/3.14 standard library, PHP 8.5 standard library/cURL/sendmail, Xserver Mail API, FTPS/SSH validation, macOS Keychain launcher, shell test runner.

## Global Constraints

- Real addresses, company domains, webhook URLs, production HMAC keys, API keys, mail bodies, and credentials must never enter tracked files or command output; fixtures use `example.invalid` and explicitly labeled deterministic non-secret test vectors only.
- Every private file is outside every `public_html`; health, lock, config, and journal files are regular, no-symlink, owner-bound, and mode `0600`; private directories retain mode `0700`.
- Canonical email preserves local-part case, lowercases only the ASCII domain, validates dot-atom/DNS limits, sorts bytewise, and removes exact duplicates.
- API mutations are add-before-delete, preserve unrelated rules, require complete readback, use config CAS, and resume only from an exact version 2 journal.
- A system email is suppressed only after the full version 1 HMAC wire contract verifies before parsing, dedup, health state, or webhook work.
- Webhook observations reserve monotonic sequences before network; every accepted newest result advances `last_applied_sequence`, including no-transition and sendmail-failure cases.
- Normal concurrency emits one error/recovery email per transition. Only a process crash after sendmail success and before state commit may duplicate an email.
- Health corruption fails closed for transition email but never blocks the ordinary webhook attempt or mailbox delivery.
- Implementation is strict red-green TDD. No production change is written before its covering test fails for the expected reason.

## File Responsibilities

- `manager/email_address.py`: one Python canonical-email implementation shared by manager and API client.
- `manager/scope_journal.py`: strict v1/v2 journal parsing and resumable v2 pinned-target intent.
- `manager/manage.py`: Japanese pinned-target UI, target union, transaction orchestration, key provisioning, diagnostics.
- `manager/xserver_api.py`: uses shared canonical-email validation without lowercasing local-part.
- `bin/manage-private-config.php`: validates pinned/derived target arrays and the private HMAC key during CAS.
- `src/CanonicalEmail.php`: runtime-equivalent canonical email validation.
- `src/SystemMailAuthenticator.php`: exact system-mail generation/verification wire contract.
- `src/DeliveryHealthMonitor.php`: strict state/lock/sequence/transition state machine.
- `src/SendmailClient.php`: bounded `/usr/sbin/sendmail -t -i` execution.
- `src/NotifierConfig.php`: validates and exposes HMAC key, health path, and canonical recipients.
- `src/ErrorReporter.php`, `src/DeliveryApplication.php`, `src/WebhookClient.php`, `bin/mail-to-lineworks.php`: integrate sequence reservation, outage/recovery transitions, and preflight system-mail suppression.
- `manager/release_workflow.py`: package every new runtime dependency and drive generation-pinned fixed-runtime migration; the existing generic `src/ReleaseValidator.php` remains unchanged.
- `fixed-runtime/generation-b9fd468-manifest.json`: independently pinned manifest of the currently deployed fixed generation, used only to authorize the next helper-only migration.
- `tests/python/*`, `tests/php/*`, `tests/run-all.sh`: regression, interruption, concurrency, secret, syntax, and packaging evidence.

---

### Task 1: Canonical email and private configuration schema

**Files:**
- Create: `manager/email_address.py`
- Create: `src/CanonicalEmail.php`
- Modify: `manager/manage.py`
- Modify: `manager/xserver_api.py`
- Modify: `manager/private_config_ssh.py`
- Modify: `macos/install_app.py`
- Modify: `bin/manage-private-config.php`
- Modify: `src/NotifierConfig.php`
- Test: `tests/python/test_manager.py`
- Test: `tests/python/test_xserver_api.py`
- Test: `tests/python/test_macos_installer.py`
- Test: `tests/python/test_private_config_ssh.py`
- Test: `tests/php/test_manage_private_config.php`
- Test: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: Python `canonical_email(value: object) -> str` and `canonical_email_list(value: object, *, allow_empty: bool, reject_duplicates: bool = False) -> list[str]`; interactive/legacy migration uses the default, strict persisted readback passes `True`.
- Produces: PHP `CanonicalEmail::one(mixed $value): string` and `CanonicalEmail::many(mixed $value, bool $allowEmpty): array`.
- Produces: `NotifierConfig::$systemMailHmacKey` as decoded 32 bytes and `$healthPath` derived as `<log-dir>/delivery-health.json`.

- [ ] **Step 1: Write failing Python canonicalization tests**

```python
def test_canonical_email_preserves_local_and_lowercases_domain(self):
    self.assertEqual("CaseSensitive@example.invalid", canonical_email("CaseSensitive@EXAMPLE.INVALID"))
    self.assertEqual(
        ["A@example.invalid", "a@example.invalid"],
        canonical_email_list(["a@EXAMPLE.INVALID", "A@example.invalid"], allow_empty=False),
    )
```

Add rejection cases for display names, whitespace, CR/LF, invalid dot-atom, local length 65, invalid DNS labels, domain length 254, total length 255, bools, non-lists, and duplicates after domain normalization.

- [ ] **Step 2: Run RED Python tests**

Run: `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api -v`

Expected: FAIL because `manager.email_address` and domain-only canonicalization do not exist.

- [ ] **Step 3: Implement the shared Python validator and replace both legacy validators**

Use the exact regexes already present in `manager/manage.py` and `manager/xserver_api.py`; return `local + "@" + domain.lower()`. List validation always canonicalizes, sorts, and deduplicates; `reject_duplicates=True` additionally rejects input that was not already canonical, sorted, and unique. Keep Japanese UI errors in `manage.py`, translating `CanonicalEmailError` at the boundary. Add `manager/email_address.py` to the Mac bundle's exact source/expected-file sets so the installed manager can import it without repository access.

- [ ] **Step 4: Write and run RED PHP config tests**

```php
$valid['notification_pinned_targets'] = ['CaseSensitive@example.invalid'];
$valid['notification_targets'] = ['CaseSensitive@example.invalid'];
$valid['system_mail_hmac_key'] = rtrim(strtr(base64_encode(str_repeat('k', 32)), '+/', '-_'), '=');
configCheckAccepts($valid);
configCheckRejects(array_replace($valid, ['system_mail_hmac_key' => str_repeat('A', 42)]));
configCheckRejects(array_replace($valid, ['notification_pinned_targets' => ['CaseSensitive@EXAMPLE.INVALID']]));
configCheckRejects(array_replace($valid, ['notification_pinned_targets' => ['Name <x@example.invalid>']]));
```

Run: `php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php`

Expected: FAIL because the new keys and exact canonical rules are not validated/exposed.

- [ ] **Step 5: Implement PHP canonical config validation**

Add `CanonicalEmail.php`; require a 43-character unpadded base64url key that strict-decodes to 32 bytes. The Python input boundary accepts valid mixed-case domains and canonicalizes them; the PHP persisted-config boundary rejects noncanonical, unsorted, or duplicate arrays. Require canonical sorted unique `error_recipients`, `notification_pinned_targets`, and `notification_targets` when present. Preserve unknown forward-compatible config keys in CAS. Derive health path from the validated private log path; reject `public_html` and symlinks as existing code does.

- [ ] **Step 6: Write RED tests for pre-release legacy-config migration**

Start with a legacy private config containing unsorted recipients, duplicate-after-domain-normalization values, mixed-case domains, no pinned list, and no HMAC key. Assert the manager canonicalizes, sorts, deduplicates, adds an empty pinned list plus one generated key, performs one config CAS/readback before fixed-runtime or active-locator mutation, preserves unknown fields, is idempotent, and stops on conflict without stage/API/locator writes.

Run: `python3 -m unittest tests.python.test_manager.ManagerTest.test_pre_release_config_upgrade -v`

Expected: FAIL because the pre-release config upgrade does not exist.

- [ ] **Step 7: Write RED tests for ongoing error-recipient mutation and health summary**

For add/change/delete error-recipient actions, use mixed-case domains and out-of-order input, then assert persisted recipients are canonical, byte-sorted, unique; the HMAC key and all unrelated fields remain byte-for-byte values; CAS/readback conflicts stop safely. For `PrivateConfigSsh.health_summary()`, assert the fixed helper operation accepts no caller-supplied path and returns only `state`, `changed_at`, `classification`, `next_observation_sequence`, and `last_applied_sequence`. Cover exact missing/healthy/degraded response schemas plus duplicate JSON keys, unknown/missing keys, 4097 bytes, wrong mode/owner, hardlink/symlink, unsafe parent chain, invalid timestamps, sequence ordering, bad message hash, and classification outside the allowlist; every invalid case returns one fixed redacted nonzero error.

Run: `python3 -m unittest tests.python.test_manager.ManagerTest.test_error_recipient_mutations_remain_canonical tests.python.test_private_config_ssh -v`

Expected: FAIL because ongoing mutations preserve insertion order and no fixed health-summary operation exists.

- [ ] **Step 8: Write RED aggregate-bound and zero-mutation migration tests**

Create reachable legacy config fixtures with 32 and 33 recipients, canonical To values of exactly 900 and 901 bytes, and log paths of exactly 4096 and 4097 bytes. Assert `MailManager.ensure_runtime_config()` accepts each reachable boundary value, rejects each boundary+1 with a fixed Japanese remediation, and performs zero config CAS, fixed-runtime mutation, API mutation, stage, or locator write on rejection. Add matching PHP helper and `NotifierConfig` boundary tests. For every accepted config, assert the shared worst-case formula proves required header lines are below 998 bytes and the complete signed message is below 65536 bytes; do not attempt to construct unreachable 997-byte/65536-byte config fixtures.

Run: `python3 -m unittest tests.python.test_manager.ManagerTest.test_pre_release_config_bounds_stop_before_mutation -v && php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php`

Expected: FAIL because the old helper path and manager do not enforce the new aggregate bounds.

- [ ] **Step 9: Implement config upgrade, health summary, and identical aggregate bounds**

Add `MailManager.ensure_runtime_config()` and call it before staging a release that declares the new config schema. Canonicalize current `error_recipients` and `notification_targets`, canonicalize an existing pinned list or create `[]`, and generate `secrets.token_bytes(32)` only when the key is absent. Before CAS, enforce the exact 32-recipient, To-900, log-4096, header-997, and message-65536 limits using the same fixed constants/formula as the new helper/runtime. Use the currently deployed helper's CAS/readback before replacing that helper. Do not add the production pinned target in this migration; the version 2 journal owns that later change. Route every later error-recipient mutation through the same canonical list and bound validator. Add the helper's exact strict `health-summary` operation and `PrivateConfigSsh.health_summary()` without exposing an arbitrary path or raw JSON. Implement the identical aggregate validator in the new private helper and `NotifierConfig`; persisted valid config must always generate an unfolded, re-authenticatable system message.

- [ ] **Step 10: Run GREEN focused tests and syntax checks**

Run: `python3 -m unittest tests.python.test_manager tests.python.test_xserver_api tests.python.test_macos_installer tests.python.test_private_config_ssh -v && php tests/php/test_manage_private_config.php && php tests/php/test_delivery.php && php -l src/CanonicalEmail.php && php -l src/NotifierConfig.php`

Expected: all selected tests pass and both syntax checks report no errors.

- [ ] **Step 11: Commit Task 1**

```bash
git add manager/email_address.py manager/manage.py manager/xserver_api.py manager/private_config_ssh.py macos/install_app.py bin/manage-private-config.php src/CanonicalEmail.php src/NotifierConfig.php tests/python/test_manager.py tests/python/test_xserver_api.py tests/python/test_macos_installer.py tests/python/test_private_config_ssh.py tests/php/test_manage_private_config.php tests/php/test_delivery.php
git commit -m "feat: validate private notification configuration"
```

---

### Task 2: Version 2 pinned-target journal and Mac management flow

**Files:**
- Modify: `manager/scope_journal.py`
- Modify: `manager/manage.py`
- Modify: `manager/release_workflow.py`
- Test: `tests/python/test_scope_journal.py`
- Test: `tests/python/test_manager.py`
- Test: `tests/python/test_release_workflow.py`

**Interfaces:**
- Consumes: Task 1 canonical email functions.
- Produces: `ScopeJournal.prepare_v2(..., desired_pinned, desired_targets, config_before_sha256)` and v2 state containing those exact fields.
- Produces: `MailManager.sync_targets(proposed_pinned: list[str] | None = None) -> bool` used by pinned add/change/delete and ordinary sync.

- [ ] **Step 1: Write failing journal schema and interruption tests**

```python
state = journal.prepare_v2(
    all_rules, old_rules, desired_rules,
    desired_pinned=["external-info@example.invalid"],
    desired_targets=["base@example.invalid", "external-info@example.invalid"],
    config_before_sha256="a" * 64,
)
self.assertEqual(2, state["schema_version"])
self.assertEqual(["external-info@example.invalid"], journal.read()["desired_pinned"])
```

Cover every interruption before/after add, delete, config CAS, readback, and commit; a fresh manager must resume from journal intent even when config still has the old pinned list. A non-committed v1 journal must stop with zero mutations; a committed v1 may be replaced by a new v2 transaction.

- [ ] **Step 2: Run RED journal tests**

Run: `python3 -m unittest tests.python.test_scope_journal -v`

Expected: FAIL because schema v2 and pinned intent are absent.

- [ ] **Step 3: Implement strict v2 journal read/write/recovery**

Extend exact-key validation, canonical-list validation, phase invariants, config digest validation, and readback. Do not weaken v1 validation. Store addresses only in the private mode-600 journal; never print them in generic error output.

- [ ] **Step 4: Write failing manager behavior tests**

```python
manager.add_targets()  # input external-info@example.invalid and exact Japanese confirmation
self.assertEqual(
    ["base@example.invalid", "external-info@example.invalid"],
    deployer.configs[-1]["notification_targets"],
)
self.assertEqual(["external-info@example.invalid"], deployer.configs[-1]["notification_pinned_targets"])
```

Cover list/add/change/delete multiple pinned values, auto+pinned overlap, refusal to delete an auto-only target, add-before-delete, unrelated rule identity, API/config conflicts, Japanese cancellation, and release-time key provisioning only when absent.

- [ ] **Step 5: Run RED manager tests**

Run: `python3 -m unittest tests.python.test_manager tests.python.test_release_workflow -v`

Expected: FAIL because manual target methods currently fail closed and deployment does not provision the HMAC key.

- [ ] **Step 6: Implement the pinned transaction and Japanese menu**

Expose four pinned actions in `MailManager.MENU`. Compute `desired = sorted(set(auto) | set(pinned))`, journal before API mutation, add/readback, delete/readback, CAS both target fields, verify, then commit. On resume use journal intent, not current config intent. Task 1 alone owns HMAC-key generation; this task must preserve the existing key byte-for-byte.

- [ ] **Step 7: Run GREEN focused tests**

Run: `python3 -m unittest tests.python.test_scope_journal tests.python.test_manager tests.python.test_release_workflow -v`

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add manager/scope_journal.py manager/manage.py manager/release_workflow.py tests/python/test_scope_journal.py tests/python/test_manager.py tests/python/test_release_workflow.py
git commit -m "feat: persist pinned notification targets"
```

---

### Task 3: Second-generation fixed-runtime helper migration

**Files:**
- Create: `fixed-runtime/generation-b9fd468-manifest.json`
- Modify: `manager/release_workflow.py`
- Modify: `manager/remote_validator.py`
- Modify: `macos/install_app.py`
- Test: `tests/python/test_release_workflow.py`
- Test: `tests/python/test_remote_validator.py`
- Test: `tests/python/test_macos_installer.py`

**Interfaces:**
- Consumes: Task 1's updated `bin/manage-private-config.php` and the exact current-generation fixed tree.
- Produces: `_migrate_fixed_generation(current_manifest, target_entries, migration_order=("bootstrap/manage-private-config.php",))` with generation-specific backup and prefix validation.
- Preserves: the existing legacy-to-current three-file migration for installations that have not reached the deployed generation.

- [ ] **Step 1: Write RED generation-selection and asset-packaging tests**

Cover original legacy with helper absent, original legacy plus new helper, every three-file prefix with old helper, every three-file prefix with new helper, exact current generation with old helper, and final generation with new helper. Assert the Mac bundle includes the new generation manifest at an exact allowlisted path/mode/hash and rejects omission, tamper, symlink, wrong mode, or an unknown extra asset.

Run: `python3 -m unittest tests.python.test_release_workflow.ReleaseWorkflowTest.test_second_generation_helper_migration tests.python.test_macos_installer.InstallerTests.test_bundle_contains_fixed_generation_manifest -v`

Expected: FAIL because the generation manifest, dual helper prefix families, and bundled asset do not exist.

- [ ] **Step 2: Capture and independently pin the current generation**

Generate a canonical manifest from the tracked bytes that match deployed release `b9fd468`: current `ReleaseValidator.php`, `validate-release.php`, stable bootstrap, wrapper, vendor tree, and old-helper metadata from the existing pinned constants. The manifest stores no old helper body. Add constants for its exact file mode, size, and SHA-256 and verify them through the existing stable-descriptor reader. Bundle the manifest through `macos/install_app.py` exact source/expected sets.

- [ ] **Step 3: Write RED crash-resume and trust tests**

Inject process abort before/after backup staging, backup publish, each three-file replacement, helper atomic replace, and final readback. A fresh workflow must accept only one exact prefix family and the corresponding exact generation-specific backup. Cover manifest tamper, wrong local mode/owner, lstat/open identity change, backup tamper, live helper tamper, ambiguous rename result, and unrelated fixed-file change.

- [ ] **Step 4: Implement the dual-prefix migration chain**

Recognize two explicit three-file prefix families: old-helper and new-helper. A helper-absent legacy tree provisions the reviewed new helper and joins the new-helper family. An old-helper prefix finishes the three-file migration without replacing its helper, reaches the independently pinned current generation, then performs the helper-only generation. A new-helper prefix finishes the three-file migration directly to the final generation. The old helper body is never bundled: current-generation migration reads the exact live old-helper bytes only after hash/mode/identity verification and stores them in a new generation-specific backup before atomic replacement. Unknown helper bytes or a mixed family stop with zero mutation.

- [ ] **Step 5: Enforce backup and prefix readback**

Keep old/new helper prefix inventories separate. Build a new sibling backup containing the exact current helper before replacement; never reuse the old three-file backup. Before every mutation, revalidate the complete live prefix and the correct backup through SSH plus FTPS hash/mode readback. Accept only reviewed old or new helper bytes after an ambiguous atomic replace.

- [ ] **Step 6: Run GREEN fixed-runtime tests**

Run: `python3 -m unittest tests.python.test_release_workflow tests.python.test_remote_validator tests.python.test_macos_installer -v`

Expected: all generation, crash-resume, identity, and fixed-runtime tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add fixed-runtime/generation-b9fd468-manifest.json manager/release_workflow.py manager/remote_validator.py macos/install_app.py tests/python/test_release_workflow.py tests/python/test_remote_validator.py tests/python/test_macos_installer.py
git commit -m "feat: migrate second-generation private config helper"
```

---

### Task 4: Authenticated system-mail wire contract

**Files:**
- Create: `src/SystemMailAuthenticator.php`
- Create: `src/SendmailClient.php`
- Create: `src/SendmailProcessAdapter.php`
- Create: `src/NativeSendmailProcessAdapter.php`
- Test: `tests/php/test_system_mail.php`
- Test: `tests/php/test_sendmail_client.php`

**Interfaces:**
- Consumes: decoded 32-byte `NotifierConfig::$systemMailHmacKey` and canonical error recipients.
- Produces: pure `SystemMailAuthenticator::build(string $type, array $recipients, string $date, string $eventId, string $body): string` and `isAuthentic(string $raw): bool`; callers inject date and 32-lowercase-hex Event ID.
- Produces: `SendmailClient::send(string $message, int $timeoutSeconds = 15): void` backed by an injected `SendmailProcessAdapter`; no reporter/delivery behavior changes in this task.
- `SendmailProcessAdapter` exposes `start(array $argv): SendmailProcessHandle`; the handle exposes `writeStdin`, `closeStdin`, `readStdout`, `readStderr`, `status`, `terminate`, and `close` operations without returning captured process output in exceptions.

- [ ] **Step 1: Write failing exact-wire and forgery tests**

```php
$wire = $auth->build('error', ['operator@example.invalid'], 'Mon, 13 Jul 2026 12:00:00 +0000', str_repeat('a', 32), "Failure\n");
systemCheck($auth->isAuthentic($wire), 'generated wire must verify');
systemCheck(!$auth->isAuthentic(str_replace("Failure\r\n", "Changed\r\n", $wire)), 'body mutation must fail');
systemCheck(!$auth->isAuthentic("X-Xserver-Mail-Notifier-Version: 1\r\n\r\nbody"), 'fixed header alone must fail');
```

Add byte-vector assertions for header names/order, unpadded key parsing, event hex, body LF normalization including terminal newline, field length framing, lowercase HMAC hex, duplicate/missing/folded headers, wrong Type/Subject, Date/To mutation, body replay, and invalid UTF-8. Use pure synthetic raw-message vectors to prove a required header line of 997 bytes is accepted while 998 is rejected, and an authenticated header prefix of exactly 65536 bytes is scanned while 65537 is not; these vectors do not pass through config validation.

- [ ] **Step 2: Run RED system-mail tests**

Run: `php tests/php/test_system_mail.php`

Expected: FAIL because `SystemMailAuthenticator` does not exist.

- [ ] **Step 3: Implement the pure authenticator**

Follow the spec wire bytes exactly. Parse only the bounded RFC 5322 header prefix, reject duplicate required headers case-insensitively, normalize body CRLF/lone CR to LF without trimming, use `hash_hmac(..., false)` and `hash_equals`. Date and Event ID are arguments so exact byte vectors require no wall clock or RNG.

- [ ] **Step 4: Write RED bounded-sendmail adapter tests**

Use a fake `SendmailProcessAdapter` to simulate partial stdin writes, 64 KiB stdout/stderr, a process that exits zero, exits nonzero, hangs until timeout, ignores first terminate, and requires final reap. Use pure synthetic message bytes to prove exactly 65536 bytes is accepted and 65537 is rejected. Assert the client uses only `['/usr/sbin/sendmail', '-t', '-i']`, drains all process output while retaining at most 8192 bytes per stream without exposing those bytes, closes all pipes, terminates at 15 seconds using an injected monotonic clock, reaps after termination, and throws only fixed messages.

- [ ] **Step 5: Implement the adapter and run GREEN tests**

`NativeSendmailProcessAdapter` owns `proc_open`, nonblocking pipe status/read/write, `proc_terminate`, and `proc_close`; `SendmailClient` owns the bounded loop and fixed timeout policy. Test code never changes process ownership or invokes a real mail transport.

Run: `php tests/php/test_system_mail.php && php tests/php/test_sendmail_client.php && php -l src/SystemMailAuthenticator.php && php -l src/SendmailClient.php && php -l src/NativeSendmailProcessAdapter.php`

Expected: all selected tests pass and syntax is valid.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/SystemMailAuthenticator.php src/SendmailClient.php src/SendmailProcessAdapter.php src/NativeSendmailProcessAdapter.php tests/php/test_system_mail.php tests/php/test_sendmail_client.php
git commit -m "feat: authenticate notifier system mail"
```

---

### Task 5: Monotonic webhook health state and recovery notification

**Files:**
- Create: `src/DeliveryHealthMonitor.php`
- Create: `src/PrivateStateFilesystem.php`
- Create: `src/NativePrivateStateFilesystem.php`
- Modify: `src/WebhookClient.php`
- Modify: `src/ErrorReporter.php`
- Modify: `src/DeliveryApplication.php`
- Modify: `bin/mail-to-lineworks.php`
- Modify: `src/OperationalLogger.php`
- Test: `tests/php/test_health_monitor.php`
- Test: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: `reserveObservation(): ?int`, `recordSuccess(int $sequence): void`, `recordFailure(int $sequence, string $classification, string $hash): void`, and `reserveSyntheticFailure(): ?int`.
- Consumes: Task 4 authenticator/sendmail. `DeliveryHealthMonitor::__construct(string $statePath, array $recipients, string $logPath, SystemMailAuthenticator $authenticator, SendmailClient $sendmail, OperationalLogger $logger, PrivateStateFilesystem $filesystem, callable $utcClock, callable $eventId)` receives every dependency explicitly; tests inject every nondeterministic boundary.
- `PrivateStateFilesystem` exposes `withExclusiveLock(string $lockPath, callable $operation): mixed`, `readRegular(string $path, int $limit): ?string`, and `replaceAtomic(string $path, string $bytes, int $mode): void`; native methods enforce identity/mode/owner/readback, while the fake deterministically injects each storage fault.
- `WebhookClient::sendObserved(...)` returns `ObservedWebhookResult(WebhookResult $result, ?int $sequence)`. One logical `send()` reserves one sequence immediately before its first HTTP request. All 429 retries and chunk requests belong to that one observation; only the terminal logical result is applied. If payload construction fails before HTTP, one synthetic sequence is reserved for the terminal `invalid_payload` result.

- [ ] **Step 1: Write failing strict-state tests**

Create a private temporary directory with exact modes and assert missing-state initialization bytes contain `next_observation_sequence=0` and `last_applied_sequence=0`. The first reservation increments, commits, and returns `1`; a state at `PHP_INT_MAX-1` returns and commits `PHP_INT_MAX`; a subsequent reservation returns null, logs `health_state_failure`, and leaves state unchanged. Also cover exact healthy/degraded key sets, duplicate JSON keys, unknown keys, oversize, symlink, mode mismatch, replaced inode, short write, failed fsync/rename/readback. Use `FakePrivateStateFilesystem` for wrong-owner and fault cases that an unprivileged test cannot create.

- [ ] **Step 2: Write failing sequence/concurrency tests**

```php
$older = $monitor->reserveObservation();
$newer = $monitor->reserveObservation();
$monitor->recordSuccess($newer);
$monitor->recordFailure($older, 'transport_error', $hash);
healthCheck($monitor->status() === 'healthy', 'older completion must not roll state back');
```

Fork or deterministically interleave two monitor instances against the same files. Cover healthy success, first double failure, degraded repeated failure, recovery on normal/error webhook success, repeated success, sendmail failure with `last_applied_sequence` advancement, next-new-sequence retry, and the documented crash window.

- [ ] **Step 3: Run RED health tests**

Run: `php tests/php/test_health_monitor.php`

Expected: FAIL because the monitor and sequence protocol do not exist.

- [ ] **Step 4: Implement strict storage and transition logic**

`NativePrivateStateFilesystem` uses lstat-open-fstat identity equality, explicit symlink/type/owner/mode checks, and stable descriptors; it does not claim an unavailable PHP `O_NOFOLLOW` flag. `DeliveryHealthMonitor` uses a mode-600 sibling lock, exclusive `flock`, maximum 4096-byte strict JSON decoding with duplicate-key rejection, atomic sibling temp/flush/fsync/readback/rename/directory-fsync/readback, and the fixed classification allowlist. Reserve before network without holding the lock during HTTP. For every newest result commit `last_applied_sequence`; update `changed_at` only after successful signed sendmail transition.

- [ ] **Step 5: Integrate normal, error, and synthetic observations**

Normal logical webhook success calls `recordSuccess`. Its terminal failure sequence is not applied; `ErrorReporter` starts a separate logical error webhook and applies only that terminal result. Intermediate retry/chunk HTTP results never update health. The token-matched forced test performs no HTTP, reserves one synthetic sequence, and calls `recordFailure(..., 'forced_test_failure', ...)`. State failures log `health_state_failure` and do not block the webhook.

- [ ] **Step 6: Verify signed email contents and secrecy**

Assert error/recovery messages contain only UTC time, allowlisted classification, 64-hex message hash, private log path, and authenticated headers. Recovery uses the classification and message hash stored by the degraded transition, not `success` or the recovering mail's hash. Assert no original From/To/Cc/Bcc/subject/body, exception text, webhook URL, or key appears. Assert one error and one recovery in normal/concurrent transitions.

- [ ] **Step 7: Write and pass preflight-order and recursion tests**

Inject counters for parser, dedup, health filesystem, webhook, and sendmail. Pass a valid authenticated system message and assert every counter remains zero except allowlisted `system_mail_suppressed` logging. Invalid, duplicate, replay-mutated, or body-mutated system headers must follow ordinary delivery. Construct the authenticator after safe config load and invoke it at the first line of `DeliveryApplication::deliver()`.

- [ ] **Step 8: Run GREEN delivery tests**

Run: `php tests/php/test_health_monitor.php && php tests/php/test_system_mail.php && php tests/php/test_delivery.php`

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 5**

```bash
git add src/DeliveryHealthMonitor.php src/PrivateStateFilesystem.php src/NativePrivateStateFilesystem.php src/WebhookClient.php src/ErrorReporter.php src/DeliveryApplication.php src/OperationalLogger.php bin/mail-to-lineworks.php tests/php/test_health_monitor.php tests/php/test_system_mail.php tests/php/test_delivery.php
git commit -m "feat: notify once on webhook outage and recovery"
```

---

### Task 6: Packaging, diagnostics, full verification, and production rollout

**Files:**
- Modify: `manager/release_workflow.py`
- Modify: `manager/manage.py`
- Modify: `macos/install_app.py`
- Modify: `README.md`
- Modify: `config/config.example.json`
- Test: `tests/python/test_release_workflow.py`
- Test: `tests/python/test_manager.py`
- Test: `tests/php/test_release_validator.php`
- Test: `tests/php/test_validate_release_entrypoint.php`
- Test: `tests/run-all.sh`

**Interfaces:**
- Consumes: all new Task 1-5 runtime files and config fields.
- Produces: packaged manifest containing every runtime dependency; Japanese diagnostics for pinned/API/config drift and health state validity; public example values only.

- [ ] **Step 1: Verify packaging coverage and write failing diagnostics tests**

Add verification assertions that the generated release manifest contains all eight new runtime files—`CanonicalEmail.php`, `SystemMailAuthenticator.php`, `SendmailClient.php`, `SendmailProcessAdapter.php`, `NativeSendmailProcessAdapter.php`, `DeliveryHealthMonitor.php`, `PrivateStateFilesystem.php`, and `NativePrivateStateFilesystem.php`—with mode `0600` and exact hashes, and that validation fails on each omission/tamper/wrong mode. These assertions may already pass through the generic plain-tree packaging and are verification, not the RED gate. The RED gate is a diagnostics test that fails because pinned drift, target drift, and health missing/healthy/degraded/invalid are not yet reported without displaying addresses, keys, message hashes, or webhook tokens.

- [ ] **Step 2: Run RED packaging tests**

Run: `python3 -m unittest tests.python.test_release_workflow tests.python.test_manager -v && php tests/php/test_release_validator.php && php tests/php/test_validate_release_entrypoint.php`

Expected: FAIL specifically on the new Japanese health/pinned diagnostics assertions; packaging verification may already pass.

- [ ] **Step 3: Implement packaging, diagnostics, and public documentation**

Pin every new source in release manifests and every new Python module in Mac app resources as required by the unchanged generic release validator. Document the private pinned-list workflow, outage/recovery semantics, crash duplicate boundary, and state diagnostic. Use only `operator@example.invalid` and `base@example.invalid` in public examples; explain that the manager generates the HMAC key and do not publish a sample key value. The secret scan must recognize the key field name without treating the field name itself as a secret.

- [ ] **Step 4: Run the full local verification gate**

Run: `bash tests/run-all.sh`

Expected: all PHP/Python tests, PHP syntax checks, Mac completion receipt verification, and public secret scan pass; only the existing explicit real-Mac SSH fixture remains skipped.

- [ ] **Step 5: Obtain task and whole-branch reviews**

Generate a review package from the pre-feature base through HEAD. Require independent reviewers to return Critical 0, Important 0, Minor 0. Fix all findings with covering tests and repeat review before deployment.

- [ ] **Step 6: Commit Task 6**

```bash
git add manager/release_workflow.py manager/manage.py macos/install_app.py README.md config/config.example.json tests/python/test_release_workflow.py tests/python/test_manager.py tests/php/test_release_validator.php tests/php/test_validate_release_entrypoint.php tests/run-all.sh
git commit -m "docs: package and operate recovery notifications"
```

- [ ] **Step 7: Perform production-safe private migration**

Read current config/API/locator hashes without printing values. Install the updated Mac app while proving its local config digest is unchanged. Before fixed-helper migration, run the idempotent pre-release config upgrade: canonical arrays, empty/existing pinned list, and HMAC key only if absent; verify by boolean/schema/digest, not values. Migrate the exact current fixed generation to the reviewed helper generation. Do not prepare or partially execute the pinned-target journal before release activation.

- [ ] **Step 8: Stage, validate, activate, and read back**

Stage a release ID derived from the reviewed commit, run strict remote validation, and switch the active locator. After activation, execute the complete v2 journal transaction whose private pinned list includes the required external forwarding source; never place that address in the repository, report, shell argv, or tool output. Read back: active commit/release, exact fixed/runtime hashes and modes, managed-filter count, pinned membership as yes/no only, config/API target equality, no probe rules, and no deployment under `public_html`.

- [ ] **Step 9: Run privacy-safe production E2E**

Use the manager's token-scoped forced test. Verify one error email, a second forced failure is suppressed, one successful observed webhook produces one recovery email, and another success is suppressed. Confirm operational log classifications/statuses and health state only through allowlisted metadata. Then run one wrapper webhook test with a new Message-ID and verify exactly one HTTP 200 event.

- [ ] **Step 10: Push only after final readback**

Run: `git status --short && git rev-parse HEAD && git push origin HEAD:main && git ls-remote origin refs/heads/main`

Expected: only known ignored SDD reports remain; local HEAD and remote `main` hashes match exactly.

---

### Addendum Task: Close one exact ambiguous version 1 delete journal

**Files:**
- Modify: `manager/manage.py`
- Modify only if needed for exact bytes readback: `manager/scope_journal.py`
- Test: `tests/python/test_manager.py`

- [ ] Record RED on the current HEAD with a successful closure fixture; it must stop at the existing version 1 rejection before implementation.
- [ ] Add the success assertion: journal-only CAS commit, clean-plan retire rules untouched, warning output contains counts only, and the same call does not prepare version 2. Reject arbitrary unknown rules; require clean-plan add=0 and exact unknown-ID/retire-ID equality.
- [ ] Add the rejection table for a proposed pinned list, pending add/non-old delete, old ID present, new ID/body/hash mismatch, config/automatic mismatch, known unrelated hash drift, duplicate API ID, cancellation, and confirmation-time journal/config/API drift.
- [ ] Write RED helper/Python tests for exact expected/desired journal CAS, missing/present expected states, lock-serialized conflict, retained-journal recovery, strict `changed`/`already_applied`/`conflict` responses, target-sync rejection by the old unconditional operation, request bound 262144, and transport/malformed/conflict reconciliation into desired success, expected not-applied, or third-state ambiguous stop.
- [ ] Cover CAS failure/readback mismatch, committed idempotency, post-write config/API drift with no rollback or additional mutation, and a separate later call that prepares a fresh version 2 journal from the current snapshot.
- [ ] Implement the smallest menu-13-only closure path with exact pre/post readbacks, the dedicated Japanese confirmation, one target-sync journal CAS, zero API/config mutation, and an immediate return. Convert every target-sync `ScopeJournal.write` to explicit raw-snapshot CAS; keep unconditional write only for filter journals.
- [ ] Pin the confirmed live 64,044-byte helper as the previous fixed generation, pin the new helper as target, and cover the consecutive helper-only migration in release workflow and Mac bundle tests.
- [ ] Run the focused manager tests, all Python/PHP tests, syntax checks, and public secret scan; self-review the diff and commit without staging the user-owned SDD reports.
