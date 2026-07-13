# Task 2 Report: Mac管理メニューのWebhook確認・変更

## Status

Implemented and verified.

## TDD evidence

- RED: `python3 -m unittest tests.python.test_manager -v`
  - Failed during import because `mask_webhook_url` did not exist. This was the expected missing-feature failure before production changes.
- GREEN (focused): `python3 -m unittest tests.python.test_manager tests.python.test_private_config_ssh tests.python.test_remote_validator -v`
  - 113 tests passed; 1 explicit real-Mac SSH credential test skipped.
- GREEN (full): `bash tests/run-all.sh`
  - 357 Python tests passed; 1 explicit real-Mac SSH credential test skipped.
  - PHP suites, syntax checks, Composer metadata checks, and public secret scan passed.

## Implementation

- Preserved menu 13 and added menu actions 14/15 with dispatch to `show_webhook_url` and `change_webhook_url`.
- Added one shared canonical LINE WORKS webhook validator and reused it for the existing connection test.
- Added complete-token masking; no short or long token substring is retained in the masked path.
- Menu 14 reads via `PrivateConfigSsh`, displays a masked value by default, and reveals the full value once only after the exact phrase `Webhook URLを表示する`.
- Menu 15 accepts the new URL only from interactive input, validates before network activity, masks confirmation output, and requires the exact phrase `Webhook URLを変更する`.
- The fixed test payload contains no URL. Only exact integer HTTP 200 proceeds to CAS; 201, 204, 400, timeout, missing status, and non-integer status fail closed.
- CAS copies the complete current object and changes only `webhook_url`, preserving unknown keys.
- `conflict` and `restored` fail without readback or unsafe client rollback. `changed` and `unchanged` require a fresh read whose hash equals `new_sha256` and whose URL equals the entered value.
- Network, CAS, post-CAS SSH, and readback failures use fixed redacted errors and never print old/new URLs.
- `_run_main` constructs `PrivateConfigSsh` from existing local metadata and passes the required expected hosts: FTPS host and server name.
- README documents menu behavior, exact reveal phrase, strict HTTP 200 gate, owner-only SSH helper, CAS conflict/conditional restore behavior, and placeholder-only examples.

## Self-review

- Verified webhook secrets are passed only as the HTTPS request target or SSH stdin-contained config object, never as subprocess argv.
- Verified no logging was added and exception chaining is suppressed at secret-bearing boundaries.
- Verified exact-status handling does not default a missing HTTP status to success.
- Verified no blind rollback exists on the Mac side.
- `git diff --check` passed.

## Note

The brief's sample assertion `assertNotIn("a", masked)` cannot coexist with the required fixed prefix because that prefix itself contains the letter `a`. The implemented regression test checks that `/message/<token>` is absent while preserving the required prefix, which directly verifies that the complete token segment is removed.

## Review follow-up

- RED reproduced that Python's `parsed.port` `ValueError` retained a hostile port token in the outer exception's `__context__` chain.
- The canonical validator now discards parser failures and raises a fixed generic `RuntimeError` outside the parser exception handler, so neither `__cause__` nor `__context__` retains the input token. Callers also wrap with fixed messages and suppress displayed chaining.
- Added regression traversal of the complete outer exception cause/context graph using `https://webhook.worksmobile.com:secret-token/message/x` and verified the token is absent from every exception.
- Added exact-status regression cases for `None`, string `"200"`, and boolean `True`; all fail before CAS.
- Added `MailManager.run()` dispatch coverage for menu choices 14 and 15.
- Follow-up focused verification: `python3 -m unittest tests.python.test_manager tests.python.test_private_config_ssh -v` ran 86 tests, all passed.
