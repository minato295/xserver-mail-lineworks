# Visible Recipient Auto-Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xserver APIから基準アドレスへの転送経路を自動検出し、表示上のTo/CcだけをLINE WORKSへ通知する管理対象ルールを安全に同期する。

**Architecture:** Python側にメールアカウント転送グラフの読み取りと期待ルール集合の計算を追加し、Mac管理CLIが差分確認後に追加先行・削除後行で同期する。Xserverルールは `header` + `contain` へ移行し、複数ルール一致による二重起動はPHP側の期限付きSHA-256ストアで抑止する。

**Tech Stack:** Python 3標準ライブラリ、Xserver Server API、PHP 8.5、JSON状態ファイル、既存FTPS/SSH配備、unittest、PHP CLIテスト。

## Global Constraints

- 実運用のドメイン名、メールアドレス、Webhook URL、サーバー名をリポジトリへ保存しない。
- 通知条件は `header` / `contain` / 完全なメールアドレス、actionは専用コマンドへの `copy` とする。
- 表示名付きTo、複数宛先To、Ccを通知し、Bccは通知しない。
- 基準アドレスへ直接または多段転送で到達する同一サーバー内アカウントだけを対象にする。
- 無関係なXserver振り分けルールは変更しない。
- 書き込み前に日本語の差分と明示確認を要求し、API読み戻しで検証する。
- 読み取り不完全、競合、空の期待集合ではフェイルクローズする。
- 秘密情報とメール内容をログへ出さず、状態は `public_html` 外、ディレクトリ700、ファイル600に置く。
- Message-IDそのものは保存せずSHA-256だけを保存する。

---

### Task 1: 転送グラフと新しい管理対象ルール

**Files:**
- Modify: `manager/xserver_api.py`
- Test: `tests/python/test_xserver_api.py`

**Interfaces:**
- Produces: `XServerApi.list_mail_accounts(domain: str) -> list[str]`
- Produces: `XServerApi.list_forwarding_addresses(address: str) -> list[str]`
- Produces: `XServerApi.discover_forwarding_sources(base_address: str) -> list[str]`
- Changes: `XServerApi.is_managed_filter(rule)` accepts only `header` + `contain` + full address + managed copy command.

- [ ] **Step 1: Write failing API and graph tests**

Add tests with a fake transport asserting the documented URLs and a graph test equivalent to:

```python
graph = {
    "info@example.invalid": [],
    "former@example.invalid": ["info@example.invalid"],
    "legacy@example.invalid": ["former@example.invalid"],
    "cycle-a@example.invalid": ["cycle-b@example.invalid"],
    "cycle-b@example.invalid": ["cycle-a@example.invalid"],
}
self.assertEqual(
    ["former@example.invalid", "info@example.invalid", "legacy@example.invalid"],
    api.discover_forwarding_sources("info@example.invalid"),
)
```

Also assert that external destinations are not returned, malformed/duplicate account data raises `XServerApiError`, and old `to` + `match` rules are not classified as the new managed shape.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.python.test_xserver_api -v`

Expected: FAIL because the three forwarding methods do not exist and the managed shape is still `to` + `match`.

- [ ] **Step 3: Implement the documented endpoints and reverse reachability**

Use fixed-origin URLs only:

```python
def list_mail_accounts(self, domain):
    url = self._mail_collection_url + "?" + urlencode({"domain": domain})
    # Validate every returned mail_address as a unique full address.

def list_forwarding_addresses(self, address):
    url = self._mail_collection_url + "/" + quote(address, safe="") + "/forwarding"
    # Validate forwarding_addresses as a duplicate-free list of full addresses.

def discover_forwarding_sources(self, base_address):
    # Load every same-domain account, build source -> destinations, then repeatedly
    # add sources whose destination is already reachable. Include base_address,
    # terminate on cycles, and return sorted canonical addresses.
```

Update `is_managed_filter` to require `field == "header"` and `match_type == "contain"`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python3 -m unittest tests.python.test_xserver_api -v`

Expected: all `test_xserver_api` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/xserver_api.py tests/python/test_xserver_api.py
git commit -m "feat: discover Xserver forwarding notification targets"
```

### Task 2: 管理CLIの診断と安全な自動同期

**Files:**
- Modify: `manager/manage.py`
- Modify: `manager/scope_journal.py`
- Test: `tests/python/test_manager.py`
- Test: `tests/python/test_scope_journal.py`

**Interfaces:**
- Consumes: `discover_forwarding_sources(base_address) -> list[str]` from Task 1.
- Produces: `MailManager.expected_targets() -> list[str]`
- Produces: `MailManager.plan_target_sync() -> dict[str, list]`
- Produces: `MailManager.sync_targets() -> bool`
- Config consumes: `notification_base_address: str`.

- [ ] **Step 1: Write failing reconciliation tests**

Cover these exact cases:

```python
# Existing: base only. Expected: base + former.
# Output must show "+ former@example.invalid" before confirmation.
# Confirmation other than "通知対象を同期する" performs no writes.
# Confirmed sync adds and reads back the new rule before deleting stale rules.
# A readback mismatch leaves old rules untouched and raises safely.
# Config notification_targets is updated only after API reconciliation succeeds.
# A changed remote config aborts without writing.
# Unrelated rules remain byte-for-byte present.
```

Change all rule fixtures to `header` + `contain`. Add journal tests showing an interrupted add or delete can resume without authorizing an unknown rule.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v`

Expected: FAIL because expected-target discovery and sync actions do not exist.

- [ ] **Step 3: Implement expectation, dry-run diagnosis, and confirmed sync**

Build rules only through:

```python
def _rule(self, address):
    return {
        "domain": address.rsplit("@", 1)[1],
        "conditions": [{"field": "header", "match_type": "contain", "keyword": address}],
        "action": {"type": "mail_address", "target": self.command_target, "method": "copy"},
    }
```

`show_diagnostics()` must calculate the live forwarding closure and print additions/deletions without writes. Add menu item `13. 通知対象を転送設定から同期`; `sync_targets()` prints the same diff and requires the exact phrase `通知対象を同期する`. Reuse/extend the scope journal so additions are read back before stale managed rules are deleted, then update `notification_targets` with a readback check.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python3 -m unittest tests.python.test_manager tests.python.test_scope_journal -v`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/manage.py manager/scope_journal.py tests/python/test_manager.py tests/python/test_scope_journal.py
git commit -m "feat: safely sync visible notification recipients"
```

### Task 3: PHPの期限付き重複通知抑止

**Files:**
- Create: `src/DeliveryDeduplicator.php`
- Modify: `src/NotifierConfig.php`
- Modify: `src/DeliveryApplication.php`
- Modify: `bin/mail-to-lineworks.php`
- Modify: `config/config.example.json`
- Test: `tests/php/test_delivery.php`
- Test: `tests/php/test_release_validator.php`

**Interfaces:**
- Produces: `DeliveryDeduplicator::__construct(string $path, int $ttlSeconds = 600)`
- Produces: `DeliveryDeduplicator::claim(string $messageIdHash, ?DateTimeImmutable $now = null): bool`
- Config produces: `dedup_path` as an absolute path outside `public_html`.

- [ ] **Step 1: Write failing deduplication tests**

Test that the first claim returns true, a second claim inside 600 seconds returns false, a claim after expiry returns true, only 64-character lowercase SHA-256 keys appear in the JSON file, mode is 0600, and a malformed/symlink/public path is rejected. In `DeliveryApplication`, deliver identical raw mail twice and assert one webhook request; deliver different Message-IDs and assert two requests. Simulate an unwritable store and assert delivery proceeds once with a safe failure log rather than dropping the notification.

- [ ] **Step 2: Run tests and verify RED**

Run: `php tests/php/test_delivery.php && php tests/php/test_release_validator.php`

Expected: FAIL because `DeliveryDeduplicator` and `dedup_path` do not exist.

- [ ] **Step 3: Implement atomic private state and integrate before webhook send**

`claim()` must lock a sibling lock file with `flock(LOCK_EX)`, reject symlinks, read a bounded JSON object, prune expired timestamps, atomically replace the state file, chmod it to 0600, and return false for an unexpired hash. `DeliveryApplication` invokes `claim()` after parsing and before formatting/webhook delivery. Construct it in the entrypoint from validated config. Do not log paths, raw IDs, headers, or exception text.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `php tests/php/test_delivery.php && php tests/php/test_release_validator.php`

Expected: both scripts exit 0 with PASS output.

- [ ] **Step 5: Commit**

```bash
git add src/DeliveryDeduplicator.php src/NotifierConfig.php src/DeliveryApplication.php bin/mail-to-lineworks.php config/config.example.json tests/php/test_delivery.php tests/php/test_release_validator.php
git commit -m "feat: suppress duplicate LINE WORKS deliveries"
```

### Task 4: 配備検証、Macアプリ更新、本番同期

**Files:**
- Modify: `README.md`
- Modify: `macos/install_app.py` only if packaged resource expectations change
- Test: `tests/python/test_macos_installer.py`
- Test: `tests/run-all.sh`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: documented menu item 13, installed Mac app, staged release, confirmed production target sync.

- [ ] **Step 1: Add failing documentation/package assertions**

Assert the installed app contains the updated manager modules and README documents: visible To/Cc matching, Bcc exclusion, automatic forwarding-source discovery, dry-run diagnostics, exact confirmation phrase, and rollback behavior. Use only example.invalid data.

- [ ] **Step 2: Run package tests and verify RED**

Run: `python3 -m unittest tests.python.test_macos_installer -v`

Expected: FAIL until documentation/package expectations match the new behavior.

- [ ] **Step 3: Update README and package resources**

Document menu item 13 and the operational sequence. Ensure no real host, domain, account, webhook token, home directory, or personal address is introduced.

- [ ] **Step 4: Run complete verification**

Run: `./tests/run-all.sh`

Expected: all Python and PHP tests PASS with no warnings or secrets in output.

- [ ] **Step 5: Commit local implementation**

```bash
git add README.md macos/install_app.py tests/python/test_macos_installer.py
git commit -m "docs: explain forwarding-aware notification sync"
```

- [ ] **Step 6: Stage and validate the production release**

Use the existing release workflow to upload into `private/xserver-mail-lineworks/releases/<release-id>`, verify manifest and PHP config check over SSH, verify directories are 0700 and files 0600 except executable entrypoints, and confirm no file exists under any `public_html`.

- [ ] **Step 7: Switch release and synchronize filters with explicit confirmation**

Use Mac menu 12 to switch the validated release, then menu 9 to confirm the dry-run. Use menu 13, review the redacted diff, and enter `通知対象を同期する`. Read back the API and remote config; abort and roll back on any mismatch.

- [ ] **Step 8: Run four end-to-end messages**

Send synthetic messages using example-only content for: direct To, display-name To, Cc, and Bcc. Expected notifications: first three exactly once; Bcc zero. Confirm mailbox copies remain for all four, webhook logs contain only allowlisted hashes/status, and the error fallback remains silent.

- [ ] **Step 9: Publish**

Run secret/PII scans, push `main`, verify GitHub HEAD equals local HEAD, and reinstall the Mac app from that verified commit.
