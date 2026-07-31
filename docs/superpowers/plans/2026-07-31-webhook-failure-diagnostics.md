# LINE WORKS Webhook障害診断ログ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LINE WORKS Webhookの各送信試行について、安全化した応答メタデータと再試行結果をprivate運用ログへ保存し、Mac管理CLIから日本語で最新原因を確認できるようにする。

**Architecture:** `WebhookClient`が本文を含まない不変の診断値を生成し、`DeliveryApplication`と`ErrorReporter`が`OperationalLogger`へ渡す。`OperationalLogger`は許可済みスキーマを再検証して0600のJSON Linesへ追記し、Mac管理CLIはFTPSで権限確認後に末尾の有効イベントだけを解析・表示する。

**Tech Stack:** PHP 8.5、Python 3.13、JSON Lines、既存のFTPS/SSH管理層、独自PHP/Pythonテストランナー。

## Global Constraints

- 本番ログは`/home/s3710/mail-lineworks/private/log/mail-notifier.jsonl`を使用し、`public_html`配下へ保存しない。
- ログファイルは`0600`、親ディレクトリは`0700`を必須とする。サーバー実行時は実効UID所有者まで検証し、Mac管理CLIではXserver本人の認証済みFTPS接続と本人ホーム配下の固定private領域を所有者境界とする。
- メール本文、通知本文、件名、差出人、To、Cc、Bcc、添付名、Webhook URL、認証情報、応答本文原文、例外メッセージを保存しない。
- `provider_code`は最大64文字、`provider_description`は最大200文字、`response_content_type`は最大100文字とする。
- 旧形式ログを読み続けられる後方互換性を維持する。
- 診断ログ障害で受信メール処理を停止させないfail-openを維持する。
- 夜間に本番配備、Webhookテスト、テストメール送信を行わない。
- 外部依存ライブラリを追加しない。

---

### Task 1: Webhook試行診断値と5xx再試行履歴

**Files:**
- Modify: `src/WebhookClient.php`
- Modify: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: `WebhookAttemptDiagnostic`（HTTP状態、provider応答、安全な応答メタデータ）。
- Produces: `WebhookDiagnostic`（試行一覧、ペイロード寸法、再試行復旧フラグ）。
- Extends: `WebhookResult::__construct(bool $success, ?int $httpStatus, string $classification, ?WebhookDiagnostic $diagnostic = null)`。
- Preserves: `WebhookResult::isSuccess(): bool`、`ObservedWebhookResult`、既存の429・400分割・transport_error動作。

- [ ] **Step 1: 失敗する診断値テストを書く**

`tests/php/test_delivery.php`へ次の振る舞いを追加する。

```php
$diagnosticResponses = [
    [
        'status' => 500,
        'body' => "{\"code\":\"E500\",\"description\":\"temporary\\u0000 failure\"}",
        'headers' => ['Content-Type' => 'application/json; charset=UTF-8'],
    ],
    response(200, 'success'),
];
$diagnosticClient = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function () use (&$diagnosticResponses): array {
        return array_shift($diagnosticResponses);
    },
    256,
    static function (): void {},
);
$diagnosticResult = $diagnosticClient->send('題名', '本文');
deliveryCheck($diagnosticResult->isSuccess(), 'HTTP 5xx retry must recover');
deliveryCheck($diagnosticResult->diagnostic instanceof WebhookDiagnostic, 'Webhook result must expose diagnostics');
deliveryCheck($diagnosticResult->diagnostic->attemptHttpStatuses() === [500, 200], 'Diagnostics must preserve both statuses');
deliveryCheck($diagnosticResult->diagnostic->recoveredByRetry, 'Retry recovery must be explicit');
deliveryCheck($diagnosticResult->diagnostic->attempts[0]->providerCode === 'E500', 'Provider code must be preserved safely');
deliveryCheck($diagnosticResult->diagnostic->attempts[0]->providerDescription === 'temporary failure', 'Controls must be removed');
```

非JSON応答とtransport例外について、`responseFormat`がそれぞれ`invalid_json`と`transport_error`になり、応答本文や例外文字列を公開プロパティに保持しないことも追加する。

- [ ] **Step 2: REDを確認する**

Run: `/opt/homebrew/bin/php tests/php/test_delivery.php`

Expected: `WebhookDiagnostic`が未定義、または`WebhookResult`に`diagnostic`がなくFAIL。

- [ ] **Step 3: 最小実装を書く**

`src/WebhookClient.php`にreadonly値オブジェクトを追加する。

```php
final class WebhookAttemptDiagnostic
{
    public function __construct(
        public readonly ?int $httpStatus,
        public readonly int|string|null $providerCode,
        public readonly ?string $providerDescription,
        public readonly string $responseFormat,
        public readonly ?string $responseContentType,
        public readonly int $responseBodyBytes,
        public readonly ?string $responseBodySha256,
    ) {}
}

final class WebhookDiagnostic
{
    /** @param list<WebhookAttemptDiagnostic> $attempts */
    public function __construct(
        public readonly array $attempts,
        public readonly int $payloadBytes,
        public readonly int $titleCharacters,
        public readonly int $textCharacters,
        public readonly bool $recoveredByRetry,
    ) {}

    /** @return list<?int> */
    public function attemptHttpStatuses(): array
    {
        return array_map(static fn (WebhookAttemptDiagnostic $item): ?int => $item->httpStatus, $this->attempts);
    }
}
```

`request()`は`WebhookAttemptDiagnostic`を返し、`requestWithRateLimitRetry()`は全試行を保持する。`sendObserved()`でタイトル・本文の文字数とJSONバイト数を結合し、最終`WebhookResult`へ診断値を設定する。JSON応答の安全化は制御文字除去と文字数上限を同じクラス内のprivate関数で行う。transport例外では例外メッセージを捨て、本文サイズ0、ハッシュ`null`とする。

- [ ] **Step 4: GREENを確認する**

Run: `/opt/homebrew/bin/php tests/php/test_delivery.php`

Expected: `PASS: delivery and fallback`

- [ ] **Step 5: コミットする**

```bash
git add src/WebhookClient.php tests/php/test_delivery.php
git commit -m "feat: capture safe webhook attempt diagnostics"
```

---

### Task 2: 診断ログの許可スキーマ・権限・呼び出し経路

**Files:**
- Modify: `src/OperationalLogger.php`
- Modify: `src/DeliveryApplication.php`
- Modify: `src/ErrorReporter.php`
- Modify: `tests/php/test_delivery.php`
- Modify: `tests/php/test_health_monitor.php`

**Interfaces:**
- Consumes: Task 1の`WebhookDiagnostic`。
- Extends: `OperationalLogger::log(string $outcome, string $messageIdHash, string $classification, ?int $httpStatus, ?WebhookDiagnostic $diagnostic = null): void`。
- Preserves: 診断値を渡さない既存呼び出しと旧5フィールドJSON Lines。

- [ ] **Step 1: 失敗するログスキーマ・秘密情報・権限テストを書く**

`tests/php/test_delivery.php`で500→200の結果を`OperationalLogger`へ渡し、JSONが次の許可済み値を持つことをリテラルで確認する。

```php
deliveryCheck($event['attempt_count'] === 2, 'Log must record bounded attempt count');
deliveryCheck($event['attempt_http_statuses'] === [500, 200], 'Log must record status history');
deliveryCheck($event['provider_code'] === 'E500', 'Log must record safe provider code');
deliveryCheck($event['provider_description'] === 'temporary failure', 'Log must record safe provider description');
deliveryCheck($event['response_format'] === 'json', 'Log must record response format');
deliveryCheck($event['recovered_by_retry'] === true, 'Log must identify retry recovery');
```

ログ文字列にfixtureのメール本文、アドレス、Webhookトークン、例外メッセージ、応答本文全体が含まれないことを確認する。新規ログのmodeが`0600`であること、既存`0644`ファイル・シンボリックリンク・`0700`でない親を拒否することを一時ディレクトリで検証する。

- [ ] **Step 2: REDを確認する**

Run: `/opt/homebrew/bin/php tests/php/test_delivery.php`

Expected: `OperationalLogger::log()`が診断引数を受け取れずFAIL。

- [ ] **Step 3: 許可済み診断スキーマと安全な追記を実装する**

`OperationalLogger`は診断値をプリミティブへ変換し、次の項目だけを追記する。

```php
$event += [
    'attempt_count' => count($diagnostic->attempts),
    'attempt_http_statuses' => $diagnostic->attemptHttpStatuses(),
    'provider_code' => $lastRelevant->providerCode,
    'provider_description' => $lastRelevant->providerDescription,
    'response_format' => $lastRelevant->responseFormat,
    'response_content_type' => $lastRelevant->responseContentType,
    'response_body_bytes' => $lastRelevant->responseBodyBytes,
    'response_body_sha256' => $lastRelevant->responseBodySha256,
    'payload_bytes' => $diagnostic->payloadBytes,
    'title_characters' => $diagnostic->titleCharacters,
    'text_characters' => $diagnostic->textCharacters,
    'recovered_by_retry' => $diagnostic->recoveredByRetry,
];
```

初回失敗後に成功した場合は、`provider_code`、`provider_description`、応答メタデータに「最後の失敗試行」を使う。通常一発成功の場合は唯一の試行を使う。

追記前に`dirname($path)`が通常ディレクトリ・所有者一致・mode`0700`であることを確認する。ファイルが存在すれば通常ファイル・所有者一致・mode`0600`を確認する。新規作成時は一時的に`umask(0077)`を設定して作成後mode`0600`を再確認し、シンボリックリンクを拒否する。

- [ ] **Step 4: `DeliveryApplication`と`ErrorReporter`から診断値を渡す**

`DeliveryApplication`の通常通知ログでは`$result->diagnostic`を第5引数へ渡す。`ErrorReporter::safeLog()`へ`?WebhookDiagnostic`を追加し、エラー通知Webhookの結果にも診断値を渡す。強制テストやWebhook未実行の場合は`null`を渡す。

- [ ] **Step 5: PHP回帰テストを実行する**

Run: `/opt/homebrew/bin/php tests/php/test_delivery.php && /opt/homebrew/bin/php tests/php/test_health_monitor.php`

Expected: 両方PASS。

- [ ] **Step 6: コミットする**

```bash
git add src/OperationalLogger.php src/DeliveryApplication.php src/ErrorReporter.php tests/php/test_delivery.php tests/php/test_health_monitor.php
git commit -m "feat: persist private webhook failure diagnostics"
```

---

### Task 3: Mac管理CLIの安全な最新原因表示

**Files:**
- Modify: `manager/manage.py`
- Modify: `manager/ftps_deployer.py`
- Modify: `tests/python/test_manager.py`
- Modify: `tests/python/test_ftps_deployer.py`

**Interfaces:**
- Produces: `FtpsDeployer.read_private_log_tail(remote_path: str, *, limit: int, expected_mode: str = "600") -> bytes`。
- Produces: `MailManager._latest_webhook_diagnostic(config: dict) -> dict[str, str]`。
- Extends: `_default_diagnostics()`の戻り値に`webhook_diagnostic`を追加する。

- [ ] **Step 1: 失敗するFTPS権限付き末尾読み取りテストを書く**

`tests/python/test_ftps_deployer.py`へ、同一TLS保護接続でMLST mode`0600`を検証してから最大256 KiBのログを取得するテストを追加する。mode`0644`、複数MLST entry、過大応答、`public_html`を含むパスは読み取り前に拒否する。

- [ ] **Step 2: REDを確認する**

Run: `python3 -m unittest tests.python.test_ftps_deployer -v`

Expected: `read_private_log_tail`が存在せずERROR。

- [ ] **Step 3: FTPS読み取り境界を実装する**

`read_private_log_tail()`は既存の`_validate_private()`、`_verify_mlst_mode()`、TLS data protectionを再利用する。XserverのFTPSにtail命令がないため、上限256 KiBで全体を読み、超過は固定エラーで拒否する。ログパスは`filesystem_home + "/mail-lineworks/private/"`配下だけを許可する。

- [ ] **Step 4: GREENを確認する**

Run: `python3 -m unittest tests.python.test_ftps_deployer -v`

Expected: PASS。

- [ ] **Step 5: 失敗する管理CLI表示テストを書く**

`tests/python/test_manager.py`へ新形式、旧形式、不正形式のfixtureを追加する。新形式では次の出力を確認する。

```text
Webhook診断: 2026年07月30日（木）08時10分49秒 / 失敗 / HTTP 500 → 500 / コード E500 / 説明 internal server error / 再試行 未復旧 / ID 2ec3d48e5880
```

旧形式は`Webhook診断: 詳細診断情報なし`、不正ログ・権限不正は`Webhook診断: 診断ログを安全に読み取れません`と表示する。fixture内のメールアドレス、Webhook URL、64文字ハッシュ全体が出力されないことも確認する。

- [ ] **Step 6: REDを確認する**

Run: `python3 -m unittest tests.python.test_manager.ManagerTest.test_diagnostics_display_latest_safe_webhook_failure -v`

Expected: `webhook_diagnostic`表示がなくFAIL。

- [ ] **Step 7: ログ解析と日本語表示を実装する**

`_latest_webhook_diagnostic()`は最大256 KiBを読み、末尾から最大1000行を走査する。`failure`または`recovered_by_retry=true`の最新イベントだけを選び、キー集合・型・長さ・SHA-256・日時を厳格検証する。`provider_description`は保存時に安全化済みでも表示前に再度制御文字を除去する。

`show_diagnostics()`へ次を追加する。

```python
self.output("Webhook診断: " + str(result.get(
    "webhook_diagnostic", "詳細診断情報なし"
)))
```

- [ ] **Step 8: Python回帰テストを実行する**

Run: `python3 -m unittest tests.python.test_ftps_deployer tests.python.test_manager -v`

Expected: PASS。

- [ ] **Step 9: コミットする**

```bash
git add manager/manage.py manager/ftps_deployer.py tests/python/test_manager.py tests/python/test_ftps_deployer.py
git commit -m "feat: show webhook causes in Mac diagnostics"
```

---

### Task 4: 運用文書・全体検証・日中配備準備

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-31-webhook-failure-diagnostics-design.md` only if implementation exposes a verified necessary correction.

**Interfaces:**
- Documents: Mac管理CLIメニュー9の確認方法、保存項目、保存禁止項目、権限、再発時の判断方法。

- [ ] **Step 1: READMEへ運用手順を書く**

次を日本語で追加する。

```text
1. Macアプリを開き「9. 同期診断」を選ぶ。
2. 「Webhook診断」のHTTP推移、コード、説明、再試行結果を確認する。
3. HTTP 5xxが再試行でも継続した場合は、日時・コード・説明・ID先頭12文字を添えてLINE WORKSへ問い合わせる。
4. transport_errorではXserverからLINE WORKSへのTLS・DNS・接続障害として調査する。
```

ログにメール内容・Webhook URL・認証情報を保存しないことと、privateログの絶対パス・`0600`/`0700`も明記する。

- [ ] **Step 2: 差分検査を実行する**

Run: `git diff --check`

Expected: 出力なし、exit 0。

- [ ] **Step 3: 全テストと秘密情報スキャンを実行する**

Run: `tests/run-all.sh`

Expected: 449件以上PASS、実Mac SSHの明示opt-inテストのみskip可、`PASS: public secret scan`、exit 0。

- [ ] **Step 4: 変更範囲を確認する**

Run: `git status --short && git diff --stat HEAD~3..HEAD`

Expected: 計画対象ファイルだけが変更され、秘密値・`.eml`・一時診断スクリプトが含まれない。

- [ ] **Step 5: 文書をコミットする**

```bash
git add README.md
git commit -m "docs: explain webhook failure diagnosis"
```

- [ ] **Step 6: 夜間停止条件を確認する**

現在時刻がユーザー指定の夜間である場合、本番配備、メニュー10、テストメール送信を実行せず、ローカル検証完了として報告する。日中の別工程でのみメニュー12による検証・配備と、秘密情報を含まない管理用テスト通知1回を実施する。
