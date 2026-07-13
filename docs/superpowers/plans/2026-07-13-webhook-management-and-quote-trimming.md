# Webhook Management and Quote Trimming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mac管理アプリからWebhook URLを秘密保持付きで確認・変更し、LINE WORKS通知を「差出人：件名」タイトルと引用省略済み本文で送る。

**Architecture:** Xserverのmode 0600設定は、検証・配備されたstandalone PHP helperへ固定SSHコマンドでJSON stdinを渡してread/CAS更新する。メール解析は全文`body`と通知用`notificationBody`を分離し、Formatterが通知タイトルと指定順本文を生成する。既存FTPS設定経路、メール原文、public_htmlは変更しない。

**Tech Stack:** Python 3.10+、PHP 8.1+、macOS Keychain、OpenSSH、Xserver PHP CLI、既存unittest/PHPテストランナー。

## Global Constraints

- 既存の`13. 通知対象を転送設定から同期`は番号と動作を変更しない。Webhook確認・変更は14/15とする。
- Webhook URL、token、APIキー、FTPS/SSH認証情報をGit、ログ、fixture、例外、コマンド引数へ含めない。
- 秘密設定は`public_html`外、directory 0700、file 0600を維持する。
- helper request/responseは128 KiB以下、設定bytesは64 KiB以下とする。
- `filesystem_home`は`\A/home/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z`へ完全一致させる。
- URL変更テストはHTTP status exact 200だけを成功とし、通常通知の2xx判定は変更しない。
- `MailMessage.body`は全文、`notificationBody`は通知専用引用省略本文とする。
- 通知本文順は受信日時、From、To、Cc、Bcc、添付ファイル、件名、本文とする。空の任意項目は見出しごと省略する。
- 本番wrapperは0701、固定bootstrapは0700、通常fileは0600を維持する。

---

### Task 1: SSH秘密設定read/CAS境界

**Files:**
- Create: `bin/manage-private-config.php`
- Create: `manager/private_config_ssh.py`
- Modify: `manager/remote_validator.py`
- Modify: `manager/release_workflow.py`
- Modify: `tests/python/test_release_workflow.py`
- Create: `tests/python/test_private_config_ssh.py`
- Modify: `tests/python/test_remote_validator.py`
- Create: `tests/php/test_manage_private_config.php`
- Modify: `tests/run-all.sh`

**Interfaces:**
- Produces: `PrivateConfigSsh(ssh_alias: str, filesystem_home: str, runner=bounded_subprocess_run)`
- Produces: `read() -> tuple[dict, str]` where the second value is lowercase SHA-256.
- Produces: immutable `ConfigCasResult(status: str, old_sha256: str, new_sha256: str)` where status is `changed|unchanged|conflict|restored`.
- Produces: `compare_and_swap(expected_sha256: str, updated: dict) -> ConfigCasResult`. The hashes come from the helper's exact old/stored bytes; the Mac never predicts PHP JSON serialization bytes.
- Produces: fixed helper source deployed at `<filesystem_home>/private/xserver-mail-lineworks/bootstrap/manage-private-config.php`, mode 0700.
- Produces: `RemoteValidator.run_trusted(remote_command: str, input_data: bytes, *, expected_hosts: list[str], output_limit: int = 131072) -> bytes`, which reuses the existing trust snapshots, SSH config parser, `ssh -G` checks, fixed `OPTIONS`, known_hosts, and bounded runner.

- [ ] **Step 1: Write failing Python boundary tests**

Add tests that require strict home validation, a fixed quoted remote command, bounded JSON stdin/stdout, no URL in argv/error, read schema validation, and CAS statuses:

```python
def test_private_config_ssh_rejects_shell_metacharacters():
    for home in ("/home/a b", "/home/a;id", "/home/$USER", "/home/`id`"):
        with self.assertRaises(ValueError):
            PrivateConfigSsh("safe-alias", home)

def test_compare_and_swap_sends_secret_only_on_stdin():
    client = PrivateConfigSsh("safe-alias", "/home/example", runner=runner)
    result = client.compare_and_swap("a" * 64, {"webhook_url": SECRET})
    self.assertEqual("changed", result.status)
    self.assertRegex(result.new_sha256, r"\A[a-f0-9]{64}\Z")
    self.assertNotIn(SECRET, " ".join(captured_argv))
    self.assertIn(SECRET, captured_input.decode())

def test_private_config_uses_remote_validator_trust_boundary():
    client.read()
    self.assertEqual(1, trusted_runner.calls)
    self.assertIn("StrictHostKeyChecking=yes", trusted_runner.inspected_options)
    self.assertNotIn(SECRET, trusted_runner.remote_command)
```

- [ ] **Step 2: Run Python tests and verify RED**

Run: `python3 -m unittest tests.python.test_private_config_ssh -v`

Expected: FAIL because `manager.private_config_ssh` does not exist.

- [ ] **Step 3: Write failing PHP helper tests**

The test harness must invoke the helper in an isolated owner-only directory and assert read, changed, conflict, unknown-key preservation rejection, symlink rejection, mode rejection, oversize rejection, and no secret in failure output:

```php
$request = ['schema_version' => 1, 'operation' => 'compare_and_swap',
    'expected_sha256' => hash('sha256', $oldBytes), 'config' => $updated];
$result = runHelper($request, $configPath);
check($result['status'] === 'changed', 'CAS must change matching old bytes');
check(fileperms($configPath) % 01000 === 0600, 'config must remain 0600');
check(!str_contains($result['stdout'] . $result['stderr'], 'secret-token'), 'output must not leak');
```

- [ ] **Step 4: Run PHP test and verify RED**

Run: `php tests/php/test_manage_private_config.php`

Expected: FAIL because `bin/manage-private-config.php` does not exist.

- [ ] **Step 5: Implement minimal standalone helper and Python client**

The PHP helper accepts only these request shapes and emits one bounded JSON response:

```php
['schema_version' => 1, 'operation' => 'read']
['schema_version' => 1, 'operation' => 'compare_and_swap',
 'expected_sha256' => $hash, 'config' => $object]
```

Implement single-process lstat/open/fstat/size/mode/UID/hash checks, exclusive sibling temp creation, `fflush`, `chmod(0600)`, revalidation, rename, readback, and conditional restore only while current hash equals intended new hash. The helper derives home with `dirname(__DIR__, 3)`, validates it with the strict ASCII home regex, and appends the fixed `/mail-lineworks/private/config.json`; it accepts no config path from argv, environment, or stdin. Python must build the command from `/usr/bin/php8.5` plus `shlex.quote(helper_path)` after the same strict home regex, then execute it only through `RemoteValidator.run_trusted()`.

Extract no weaker SSH path: `run_trusted()` must perform the current config/known_hosts trust snapshots, exact single-alias parsing, `ssh -G` option-injection checks, `BatchMode=yes`, `StrictHostKeyChecking=yes`, fixed known_hosts paths, disabled ProxyCommand/ProxyJump/RemoteCommand/forwarding, pre/post trust comparison, timeout, and bounded stdout/stderr. Add attacks for alias beginning `-`, Include/Match, hostile ProxyCommand, changed trust files, and unexpected resolved host.

- [ ] **Step 6: Provision helper in fixed runtime**

Add the helper to `ReleaseWorkflow.provision_fixed_runtime()`:

```python
files["bootstrap/manage-private-config.php"] = source_root / "bin/manage-private-config.php"
```

Require mode 0700 and exact SHA-256 in existing fixed-runtime inspection tests.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```bash
php tests/php/test_manage_private_config.php
python3 -m unittest tests.python.test_private_config_ssh tests.python.test_remote_validator tests.python.test_release_workflow -v
```

Expected: PASS, with no secret value printed.

- [ ] **Step 8: Commit**

```bash
git add bin/manage-private-config.php manager/private_config_ssh.py manager/remote_validator.py manager/release_workflow.py tests/php/test_manage_private_config.php tests/python/test_private_config_ssh.py tests/python/test_remote_validator.py tests/python/test_release_workflow.py tests/run-all.sh
git commit -m "feat: add SSH private config CAS helper"
```

### Task 2: Mac管理メニューのWebhook確認・変更

**Files:**
- Modify: `manager/manage.py`
- Modify: `tests/python/test_manager.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `PrivateConfigSsh.read()` and `compare_and_swap()` from Task 1.
- Produces: `mask_webhook_url(value: str) -> str`.
- Produces: `MailManager.show_webhook_url()` and `MailManager.change_webhook_url()`.
- `MailManager` receives `private_config_client` and an injected exact-status HTTP sender for tests.

- [ ] **Step 1: Write failing menu and masking tests**

```python
def test_menu_preserves_13_and_adds_webhook_actions(self):
    self.assertIn("13. 通知対象を転送設定から同期", MailManager.MENU)
    self.assertIn("14. Webhook URL確認", MailManager.MENU)
    self.assertIn("15. Webhook URL変更", MailManager.MENU)

def test_mask_never_exposes_short_or_long_token(self):
    for token in ("a", "abcd", "very-long-secret-token"):
        value = "https://webhook.worksmobile.com/message/" + token
        masked = mask_webhook_url(value)
        self.assertNotIn(token, masked)
        self.assertTrue(masked.startswith("https://webhook.worksmobile.com/message/"))
```

- [ ] **Step 2: Run focused test and verify RED**

Run: `python3 -m unittest tests.python.test_manager -v`

Expected: FAIL because menu actions and masking function are absent.

- [ ] **Step 3: Write failing transactional-change tests**

Cover masked confirmation, exact reveal phrase, cancellation, URL validation before network, exact 200 CAS followed by a fresh readback, 201/204/400/timeout no-CAS, conflict, unchanged, restored, post-CAS SSH read failure, readback mismatch, and output/exception redaction:

```python
manager.change_webhook_url()
self.assertEqual([NEW_CONFIG], private_client.cas_inputs)
self.assertEqual("changed", private_client.result.status)
self.assertEqual(2, private_client.read_calls)
self.assertEqual(NEW_URL, private_client.last_read_config["webhook_url"])
self.assertEqual(private_client.result.new_sha256, private_client.last_read_sha256)
self.assertNotIn(NEW_URL, "\n".join(output))

for status in (201, 204, 400):
    manager = make_manager(http_status=status)
    with self.assertRaises(RuntimeError):
        manager.change_webhook_url()
    self.assertEqual([], manager.private_config_client.cas_inputs)
```

- [ ] **Step 4: Implement minimal menu behavior**

Use menu mappings `14 -> show_webhook_url`, `15 -> change_webhook_url`. Validate URL with one shared canonical validator. Full reveal requires the exact runtime-displayed phrase `Webhook URLを表示する`; change requires `Webhook URLを変更する`. Test payload is fixed and contains neither URL:

```python
payload = {"title": "Webhook URL変更テスト", "body": {"text": "Mac管理アプリからの接続確認です。"}}
```

Only status `200` proceeds to CAS. Preserve every unknown config key by copying the full read object and replacing only `webhook_url`. Treat `changed` and `unchanged` as provisional success, then perform a fresh `read()` and require both its returned hash to equal `ConfigCasResult.new_sha256` and its `webhook_url` to equal the entered value before reporting completion. The helper computes hashes from exact bytes before/after its own JSON serialization; the Mac does not reproduce that serialization. `conflict` is a non-mutating retry-required failure. `restored` is a failed change with the old value recovered. Any post-CAS SSH failure or readback mismatch is a failure and must never print either URL; the helper's conditional restore semantics remain authoritative and the Mac client must not perform an unsafe blind rollback.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python3 -m unittest tests.python.test_manager tests.python.test_private_config_ssh tests.python.test_remote_validator -v`

Expected: PASS.

- [ ] **Step 6: Document operator behavior without real values**

Document menu 14/15, masked/default reveal, exact-200-before-save, SSH owner-only configuration, and rollback/conflict behavior. Use `example.invalid` or token placeholders only.

- [ ] **Step 7: Commit**

```bash
git add manager/manage.py tests/python/test_manager.py README.md
git commit -m "feat: manage LINE WORKS webhook from Mac app"
```

### Task 3: Notification title, attachment order, and quote trimming

**Files:**
- Create: `src/QuoteTrimmer.php`
- Modify: `src/MailMessage.php`
- Modify: `src/MailParser.php`
- Modify: `src/NotificationFormatter.php`
- Modify: `src/DeliveryApplication.php`
- Modify: `tests/php/test_mail_parser.php`
- Modify: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: `QuoteTrimmer::plainText(string $body): string` and `QuoteTrimmer::html(string $html): string`.
- `MailMessage` adds readonly `string $notificationBody` immediately after `body`.
- Produces: `NotificationFormatter::title(MailMessage $message): string` and existing `format()` uses `notificationBody`.

- [ ] **Step 1: Write failing quote fixtures**

Add separate assertions for `>` lines, `On ... wrote:`, Japanese dated reply boundary, Original Message, qualified Outlook block, HTML `blockquote`, whitespace-token `gmail_quote`, and quote-only fallback. Add negative fixtures for `not_gmail_quote`, isolated labels, incomplete Outlook headers, and ordinary `wrote:` prose.

```php
mailCheck(QuoteTrimmer::plainText("新規\n> 引用\n続き") === "新規\n続き", 'inline quote lines');
mailCheck(QuoteTrimmer::plainText("通常文で wrote: と説明") === "通常文で wrote: と説明", 'ordinary prose');
mailCheck(str_contains(QuoteTrimmer::html('<div class="not_gmail_quote">残す</div>'), '残す'), 'class token exactness');
```

- [ ] **Step 2: Run mail test and verify RED**

Run: `php tests/php/test_mail_parser.php`

Expected: FAIL because `QuoteTrimmer` and `notificationBody` are absent.

- [ ] **Step 3: Implement bounded quote trimming and body separation**

Implement a linear/bounded scanner for plain text and the existing bounded HTML tokenizer pattern for removal. Parser sets:

```php
$body = $plainAvailable ? self::normalizeText($plain) : self::htmlToText($html);
$notificationBody = $plainAvailable
    ? QuoteTrimmer::plainText($body)
    : self::htmlToText(QuoteTrimmer::html($html));
```

If trimming yields blank text, use `（引用部分は省略しました）`. Preserve `body` unchanged relative to current parsing.

- [ ] **Step 4: Write failing title and ordering tests**

```php
mailCheck($formatter->title($message) === '送信者：件名', 'title format');
mailCheck($formatter->title($empty) === '（差出人不明）：（件名なし）', 'title fallback');
mailCheck(mbOrder($formatted, ['Bcc：', '添付ファイル：', '件名：', '本文：']), 'attachment order');
```

Also test control removal and exactly 100 Unicode code points without requiring mbstring.

- [ ] **Step 5: Implement title and formatter order**

Move the attachment block immediately after Bcc and before subject. Sanitize From/Subject, join with `：`, and truncate with the existing UTF-8 sequence scanner so no byte sequence is split.

Change delivery to use the computed title:

```php
$formatter = new NotificationFormatter();
$result = $this->webhook->send($formatter->title($message), $formatter->format($message));
```

- [ ] **Step 6: Run focused PHP tests and verify GREEN**

Run:

```bash
php tests/php/test_mail_parser.php
php tests/php/test_delivery.php
```

Expected: PASS, including existing full-body, attachment, retry, fallback, and dedup tests.

- [ ] **Step 7: Commit**

```bash
git add src/QuoteTrimmer.php src/MailMessage.php src/MailParser.php src/NotificationFormatter.php src/DeliveryApplication.php tests/php/test_mail_parser.php tests/php/test_delivery.php
git commit -m "feat: improve LINE WORKS notification previews"
```

### Task 4: Integration, app packaging, security verification, and deployment

**Files:**
- Modify: `macos/install_app.py` only if generated menu/receipt expectations require it.
- Modify: `tests/python/test_macos_installer.py` only for new generated-bundle assertions.
- Modify: `README.md` if integration verification reveals missing operational instructions.

**Interfaces:**
- Consumes all Tasks 1-3.
- Produces a rebuilt local Mac app and a versioned Xserver release; no source-level secret values.

- [ ] **Step 1: Add failing packaging/security assertions if needed**

Assert the installed bundle exposes menu 14/15 through the bundled source, includes no Webhook-shaped token, and the fixed runtime manifest includes the helper at mode 0700.

- [ ] **Step 2: Run packaging tests and verify RED when assertions were added**

Run: `python3 -m unittest tests.python.test_macos_installer tests.python.test_release_workflow -v`

Expected: FAIL only for the newly required packaging behavior. If existing generic packaging already satisfies it, record that evidence and do not add a vacuous test.

- [ ] **Step 3: Implement the minimal packaging adjustment**

Keep credentials in Keychain, keep Webhook URL only on Xserver, and ensure generated AppleScript continues to launch the bundled CLI without embedding URL or environment secrets.

- [ ] **Step 4: Run the complete local verification suite**

Run: `./tests/run-all.sh`

Expected: all PHP/Python tests, syntax checks, installer receipt checks, and public secret scan PASS; only the documented optional real-SSH test may skip.

- [ ] **Step 5: Independent code review**

Create a review package from the pre-feature base through HEAD. Require Critical 0 and Important 0 for the SSH command boundary, CAS/rollback, secret output, quote false positives, title encoding, and attachment order before publishing.

- [ ] **Step 6: Commit any review fixes and rerun full verification**

For every Critical/Important fix, first add or tighten a failing focused test, verify RED, implement, verify focused GREEN, then rerun `./tests/run-all.sh`.

- [ ] **Step 7: Publish and reinstall**

Push `main`, reinstall `$HOME/Applications/Xserverメール通知管理.app`, and verify the completion receipt. Do not print or inspect the current full Webhook URL during installation.

- [ ] **Step 8: Deploy without weakening permissions**

Stage a new immutable release outside `public_html`, validate manifest, actual Composer parser dry-run, helper hash/mode, public boundary, and symlink count. Atomically activate the release locator. Provision the new helper by verified same-directory temporary rename; preserve config mode 0600 and wrapper mode 0701.

- [ ] **Step 9: Production acceptance**

From the Mac app, verify masked URL display. Use explicit reveal only if the user requests it at that moment. Change to a test/new URL only when supplied by the user; otherwise do not mutate the current URL. Send a synthetic reply email containing a new paragraph plus quoted history and an attachment declaration, then confirm the LINE WORKS title, field order, quote omission, HTTP 200 operational log, mailbox copy, and absence of secrets/content in logs.

- [ ] **Step 10: Final repository and server audit**

Confirm clean Git status, GitHub `main` equals local HEAD, permanent filters remain scoped, diagnostics are removed, production permissions are exact, and secret scan covers tracked/untracked source and generated app artifacts.
