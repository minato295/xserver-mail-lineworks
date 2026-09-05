# Notifier Reliability Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for implementation and independent review.

**Goal:** Fix reviewed alert, health, diagnostics, privacy, deduplication and undelivered-message recovery problems.

**Architecture:** Retain the PHP CLI delivery pipeline and authenticated Mac manager. Separate delivery health from alert transport, persist undelivered work privately, and expose deliberate operator recovery.

**Tech Stack:** PHP >=8.1, Python 3, existing MIME parser, FTPS/SSH.

**Spec:** `docs/superpowers/specs/2026-09-05-notifier-reliability-design.md`

## Global Constraints

- No real LINE WORKS or email test sends.
- Private data outside public_html; directories 0700, files 0600.
- No message bodies, addresses, secrets, exception messages or arguments in diagnostics.
- Japanese operator messages; preserve To/Cc selection and Bcc-only exclusion.
- Preserve unrelated dirty main checkout.

### Task 1: Alert, health and private diagnostics

Files: src/ErrorReporter.php, src/DeliveryApplication.php, src/DeliveryHealthMonitor.php, src/OperationalLogger.php or dedicated diagnostic writer, bin/mail-to-lineworks.php, health schema readers in manager/ and bin/manage-private-config.php; tests/php/test_delivery.php and test_health_monitor.php, affected Python schema tests.

- [x] Reproduce real application error with mocked HTTP: assert zero error HTTP requests, failure health even if sendmail fails, no alert-only recovery.
- [x] Implement pending alert state independent of delivery state; retain legacy reads and monotonic observations. Tests exercise failed error mail followed by retry and failed recovery mail followed by retry, plus concurrency.
- [x] Record safe exception stage/type/location, not message or args. Test a secret-bearing exception and verify absent secret with usable stage and location.
- [x] Run affected PHP/Python tests and commit task changes.

### Task 2: Private recovery and bounded deduplication

Files: src/DeliveryDeduplicator.php, new private outbox component and tests, src/DeliveryApplication.php, src/WebhookClient.php, CLI recovery integration.

- [x] Reproduce oversized expired dedup state; ensure it can be compacted and future duplicate suppression works. Prevent writing unreadable state.
- [x] Persist failed delivery text with idempotent key, state, and confirmed progress. List pending/uncertain work without sending; replay only explicit selected work. Preserve the current recipient allowlist and current webhook settings at retry time.
- [x] Test full failure, partial split, uncertain transport, parallel reserve, storage limits, and no sends in list/check mode with injected transports.
- [x] Document deliberate retry workflow and commit.
- [x] Address independent-review finding: bounded private directory-lock contention handling, retaining inode/ancestor identity checks and timeout refusal.

### Task 3: Public information and integration

Files: README.md, affected docs, tests/run-all.sh or extracted public scanner with tests.

- [x] Replace actual environment paths with /home/example/... in tracked documentation.
- [x] Add scanner regression for private account paths; legitimate example.invalid remains allowed.
- [ ] Run complete offline suite and public scan. Independent review and address substantive findings.
- [ ] Publish through reviewed PR and deploy using existing verified workflow; verify release readback without notification tests.
