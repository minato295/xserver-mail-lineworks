# Japanese System Alert Emails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xserverメール通知システムの障害・復旧・動作確認メールを、個人情報を漏らさない日本語文面と日本時間表示へ移行し、旧v1メールの再帰抑止を保ったまま認証形式v2で安全に送信する。

**Architecture:** 日本語文面とJST変換は新しい純粋クラス `SystemAlertFormatter` に分離する。`DeliveryHealthMonitor` は状態遷移と障害開始時刻の受け渡しだけを担当し、`SystemMailAuthenticator` はcanonical RFC 2047/MIME/Base64/HMAC v2 wireを発行する一方、受信検証では既存v1と新v2をversionごとに分岐する。内部状態・ログのUTC、状態スキーマ、sendmail成功後にだけ遷移を確定する境界は変更しない。

**Tech Stack:** PHP 8.1、Composer PSR-4、RFC 2047 encoded-word、MIME Base64、HMAC-SHA256、既存PHPスクリプトテスト、Python `unittest`、Mac管理アプリの既存release workflow。

## Global Constraints

- 実装の正本は [承認済み設計書](../specs/2026-07-14-japanese-system-alerts-design.md) とする。
- テストを先に失敗させ、最小実装で通し、各タスクの対象テストを再実行してからコミットする。
- v1の英語件名、UTC Date、HMAC field順序、本文正規化はbyte互換で維持する。
- v2 decoded本文はUTF-8、LFのみ、CR/NULなし、末尾LFちょうど1個とする。CRLFを使うのはcanonical Base64 wireだけである。
- wireとBase64復号本文の両方に、元メール、Webhook URL、鍵、例外、64桁Message-ID hash、ログ絶対パスを含めない。
- 公開リポジトリには実在するメールアドレス、ホスト、Webhook、API鍵、SSH鍵、サーバーパスを追加しない。fixtureは `example.invalid` と固定の非秘密テスト値だけを使う。
- `delivery-health.json` のschema versionとkey集合は変えず、`changed_at` とログはUTCのまま保存する。

---

### Task 1: 日本語文面とJST変換を純粋クラスとして実装する

**Files:**
- Create: `src/SystemAlertFormatter.php`
- Create: `tests/php/test_system_alert_formatter.php`
- Modify: `tests/run-all.sh`

- [ ] **Step 1: formatterの公開契約を固定する失敗テストを書く**

`tests/php/test_system_alert_formatter.php` に次の契約を置く。

```php
/** @return array{date:string,body:string,test:bool} */
$alert = $formatter->format(
    'error',
    new DateTimeImmutable('2026-07-14T08:33:55Z'),
    new DateTimeImmutable('2026-07-14T08:33:55Z'),
    'transport_error',
);
formatterCheck($alert['date'] === 'Tue, 14 Jul 2026 17:33:55 +0900', 'v2 Date must be exact JST');
formatterCheck($alert['test'] === false, 'A real transport failure must not be labelled as a test');
formatterCheck($alert['body'] === $expectedRealErrorBody, 'Real error body must match the approved copy exactly');
```

実障害、実復旧、`forced_test_failure` の障害、同復旧の4本文を設計書と完全一致で比較し、全本文が末尾LFちょうど1個であることも検証する。

- [ ] **Step 2: JSTのカレンダー境界と曜日を表駆動テストする**

少なくとも次を固定ベクタにする。

```php
$jstCases = [
    ['2026-07-13T15:00:00Z', '2026年07月14日（火）00時00分00秒'],
    ['2026-07-31T15:00:00Z', '2026年08月01日（土）00時00分00秒'],
    ['2026-12-31T15:00:00Z', '2027年01月01日（金）00時00分00秒'],
    ['2028-02-29T14:59:59Z', '2028年02月29日（火）23時59分59秒'],
    ['2028-02-29T15:00:00Z', '2028年03月01日（水）00時00分00秒'],
];
```

日曜から土曜まで7曜日も固定配列で確認し、入力 `DateTimeImmutable` のtimezone/valueが変更されないことを検証する。

- [ ] **Step 3: 全原因コードと安全な縮退を失敗テストにする**

設計書の11種類の表示を表駆動で検証する。`success`、`system_mail_suppressed`、空文字、allowlist外文字列は、本文にも管理用原因コードにも入力値を残さず `unknown` と同じ文面・コードへ縮退させる。

- [ ] **Step 4: formatterテストを実行してREDを確認する**

Run: `php -d assert.exception=1 tests/php/test_system_alert_formatter.php`

Expected: `SystemAlertFormatter` が未実装のため非0終了。

- [ ] **Step 5: `SystemAlertFormatter` を最小実装する**

公開メソッドは次に固定する。

```php
final class SystemAlertFormatter
{
    /** @return array{date:string,body:string,test:bool} */
    public function format(
        string $type,
        DateTimeImmutable $eventAtUtc,
        DateTimeImmutable $failureAtUtc,
        string $classification,
    ): array;
}
```

内部では `Asia/Tokyo` へ変換し、曜日はロケールではなく `['日','月','火','水','木','金','土']` で生成する。`type` は `error|recovery` 以外を `InvalidArgumentException` にし、原因表示はallowlistだけから選ぶ。`forced_test_failure` のときだけ `test=true` とする。

- [ ] **Step 6: runnerへ追加し、GREENと構文検査を確認する**

`tests/run-all.sh` の `test_system_mail.php` の直後に次を追加する。

```bash
php -d assert.exception=1 tests/php/test_system_alert_formatter.php
```

Run: `php -l src/SystemAlertFormatter.php && php -d assert.exception=1 tests/php/test_system_alert_formatter.php`

Expected: syntax OK、`PASS: Japanese system alert formatter contract`。

- [ ] **Step 7: Task 1をコミットする**

```bash
git add src/SystemAlertFormatter.php tests/php/test_system_alert_formatter.php tests/run-all.sh
git commit -m "feat: format system alerts in Japanese"
```

---

### Task 2: canonical MIME/HMAC v2を発行し、v1/v2を検証する

**Files:**
- Modify: `src/SystemMailAuthenticator.php`
- Modify: `tests/php/test_system_mail.php`
- Create: `tests/fixtures/system-mail-v1-postfix.eml`

- [ ] **Step 1: 既存v1検証ベクタをbuilderテストから分離する**

`syntheticSystemMail()` と既存のv1 exact-wire/HMAC/改ざんテストを残す。builderの期待だけをv2へ移す。固定fixture `tests/fixtures/system-mail-v1-postfix.eml` には、`example.invalid` のみを使った `Return-Path`、複数 `Received`、継続行、`From`、`Message-Id`、英語v1件名、UTC Date、真正HMACを含め、MIMEヘッダーは含めない。

fixtureの認証ベクタは次に固定し、実装時に別値を選ばない。

```text
key: ASCII "k" 32byte
version: 1
type: error
event: 0123456789abcdef0123456789abcdef
to: operator@example.invalid
subject: Xserver mail notifier error
date: Mon, 13 Jul 2026 12:00:00 +0000
canonical body: Legacy delivery failure\n
body SHA-256: 749adcf5a0cb086d328dab809eddadcbb609ffb8d63f6b36e87f7fcf0aea0dea
HMAC-SHA256: 39b0d5900e65631f539aa7f6679b03fe23befb968daa943d36b9509b767dbacc
```

テストではfixtureが上記配送ヘッダーを実際に含むこと、真正fixtureが認証されること、本文またはHMACの1byte改変が拒否されることを確認する。

- [ ] **Step 2: v2の独立wireベクタを先に書く**

`build()` は後方互換のため末尾引数だけを追加する。

```php
public function build(
    string $type,
    array $recipients,
    string $date,
    string $eventId,
    string $body,
    bool $test = false,
): string
```

独立テストhelperで次のfield順を直接frameし、実装のHMACとwire完全一致を比較する。

```php
$fields = [
    '2', $type, $event, $to, $encodedSubject, $date,
    '1.0', 'text/plain; charset=UTF-8', 'base64', $decodedBodyHash,
];
```

4件名すべてのbuild→authenticate、canonical Toのsort/deduplicate、`Date: Tue, 14 Jul 2026 17:33:55 +0900`、MIME3本exactly-onceを検証する。

- [ ] **Step 3: RFC 2047とBase64のcanonical性を失敗テストにする**

- SubjectをUTF-8コードポイント単位で最大45byte chunkへ分ける。
- 各 `=?UTF-8?B?...?=` は75文字以下、複数word間はASCII空白1個、header foldingなし。
- decoded本文はvalid UTF-8、NUL/CRなし、LFのみ、末尾LFちょうど1個。
- wire本文は76文字ごとのCRLF、末尾CRLFちょうど1個。
- NFC/NFDを別byte列のまま保持し、Unicode normalizationしない。
- wire全体65536byteを受理し65537byteを拒否、必須header 997byteを受理し998byteを拒否する。

- [ ] **Step 4: v2改ざん拒否を表駆動で追加する**

次を1ケースずつ拒否する。

- typeと4件名の不正ペア、raw UTF-8件名、Q encoding、別chunk、charset表記差、word間空白差。
- Dateの `+0000`、曜日不一致、存在しない日、桁不足、制御文字。
- MIME headerの欠落・重複・折返し、Content-Type/charset/CTEのexact値違反。
- Base64の不正文字、空白、padding違反、LF-only、75/77文字折返し、末尾CRLF欠落・重複。
- decoded本文の不正UTF-8、NUL、CR、末尾LFなし・2個。
- hash、HMAC、event、To、Dateの改ざん。
- versionの欠落・重複・`0`・`3`・`02`。

- [ ] **Step 5: REDを確認する**

Run: `php -d assert.exception=1 tests/php/test_system_mail.php`

Expected: 現行builderがv1を出すためv2期待で非0終了。

- [ ] **Step 6: versionごとの検証処理を実装する**

`REQUIRED_HEADERS` は共通headerだけのまま保ち、parse後に分岐する。

```php
return match ($version) {
    '1' => $this->authenticateV1($headers, $rawBody),
    '2' => $this->authenticateV2($headers, $rawBody),
    default => false,
};
```

v1は現行処理をprivate helperへ移すだけにし、v2だけがMIME3本をexactly-onceで要求する。v2 HMACはencoded Subject値とdecoded本文hashを使う。v2 Dateは `D, d M Y H:i:s +0900` の完全一致を要求する。Base64はstrict decode後、同じdecoded byte列をcanonical再encodeしたwireと完全一致させる。

- [ ] **Step 7: focused testをGREENにする**

Run: `php -l src/SystemMailAuthenticator.php && php -d assert.exception=1 tests/php/test_system_mail.php`

Expected: syntax OK、`PASS: authenticated system mail v1/v2 wire contract`。

- [ ] **Step 8: Task 2をコミットする**

```bash
git add src/SystemMailAuthenticator.php tests/php/test_system_mail.php tests/fixtures/system-mail-v1-postfix.eml
git commit -m "feat: authenticate Japanese system mail v2"
```

---

### Task 3: 障害状態遷移へ日本語メールを統合する

**Files:**
- Modify: `src/DeliveryHealthMonitor.php`
- Modify: `tests/php/test_health_monitor.php`

- [ ] **Step 1: clockを時系列注入できるfixtureへ直す**

`fakeMonitor()` がクロージャから複数UTC時刻を返せるようにし、障害を `2026-07-14T08:33:55Z`、復旧を `2026-07-14T08:35:10Z` で記録できるfixtureを用意する。

- [ ] **Step 2: 4遷移の件名・本文完全一致テストを書く**

実障害、実復旧、テスト障害、テスト復旧について、RFC 2047件名を復号し、Base64本文をstrict decodeしてTask 1の期待値と完全一致させる。復旧本文は保存済み `changed_at` を障害発生時刻に使い、「自動では再通知されません」とXserverメールボックス確認を含むことを検証する。

- [ ] **Step 3: 状態遷移とfail-open回帰テストを補強する**

- 初回障害1通、障害中追加失敗0通、初回復旧1通、健康中追加成功0通。
- 障害中の別classification/hashでも初回 `changed_at` とclassificationを復旧まで保持。
- stateの `changed_at` はUTC、復旧後のkey集合は従来どおり。
- 障害メール送信失敗はhealthy維持、復旧メール送信失敗はdegraded維持し、次観測で再試行。
- sendmail成功/state commit失敗の既存at-least-once crash windowを維持。

- [ ] **Step 4: classificationの安全な縮退をテストする**

`recordFailure()` に `success`、`system_mail_suppressed`、allowlist外値を渡すと、stateと復号本文の両方で `unknown` になることを確認する。旧stateに `system_mail_suppressed` が残っている復旧ケースも表示時に `unknown` へ縮退させる。

実装では、旧stateの読み取り互換に使うclassification集合と、新規failure入力に使う集合を分ける。

```php
private const FAILURE_CLASSIFICATIONS = [
    'invalid_payload', 'invalid_parameter', 'missing_parameter',
    'invalid_webhook_url', 'rate_limited', 'http_error', 'transport_error',
    'forced_test_failure', 'internal_error', 'health_state_failure', 'unknown',
];
```

- [ ] **Step 5: wire全体とdecoded本文のプライバシーテストを書く**

From、To、Cc、Bcc、元件名、元本文、添付名、例外sentinel、Webhook URL、HMAC鍵sentinel、ログ絶対パス、元Message-ID由来64桁hashの各値がraw wireにもdecoded本文にもないことを確認する。wireのTo headerは `operator@example.invalid` だけを許可する。既存の「hash/log pathが本文にある」というassertは逆向きへ変更する。

- [ ] **Step 6: REDを確認する**

Run: `php -d assert.exception=1 tests/php/test_health_monitor.php`

Expected: 英語平文とhash/log pathを出す現行実装のため非0終了。

- [ ] **Step 7: monitorをformatterへ接続する**

`sendTransition()` はhashを本文生成へ渡さず、復旧時にはstateの `changed_at` をUTC `DateTimeImmutable` としてparseした値を渡す。

```php
$formatted = (new SystemAlertFormatter())->format(
    $type,
    $now,
    $failureAt,
    $classification,
);
$wire = $this->authenticator->build(
    $type,
    $this->recipients,
    $formatted['date'],
    $event,
    $formatted['body'],
    $formatted['test'],
);
```

状態保存用hashは削除せず、本文とauthenticatorへ渡す経路だけをなくす。

- [ ] **Step 8: focused testをGREENにする**

Run: `php -l src/DeliveryHealthMonitor.php && php -d assert.exception=1 tests/php/test_health_monitor.php`

Expected: syntax OK、`PASS: delivery health state machine and Japanese alert contract`。

- [ ] **Step 9: Task 3をコミットする**

```bash
git add src/DeliveryHealthMonitor.php tests/php/test_health_monitor.php
git commit -m "feat: send Japanese delivery health alerts"
```

---

### Task 4: 実配送経路とrelease packagingをv1/v2対応にする

**Files:**
- Modify: `tests/php/test_delivery.php`
- Modify: `tests/php/test_release_validator.php`
- Modify: `tests/php/test_validate_release_entrypoint.php`
- Modify: `tests/python/test_release_workflow.py`
- Modify: `README.md`

- [ ] **Step 1: v1/v2再帰抑止の統合テストを先に書く**

`tests/php/test_delivery.php` で、builder生成v2と `tests/fixtures/system-mail-v1-postfix.eml` の両方が通常メール解析、dedup、Webhook、health、sendmailより前に停止し、ログが `system_mail_suppressed` だけになることを確認する。

- [ ] **Step 2: forced testと復旧の実経路テストを日本語へ更新する**

生成メールをdecodeして、日本語テスト件名・本文、日本語復旧件名・本文を完全一致で検証する。統合fixtureにはFrom/To/Cc/Bcc/件名/本文/添付名/Webhook/例外/鍵/log path/hashのsentinelを与え、raw wire、RFC 2047復号件名、Base64復号本文のすべてで非包含を確認する。

- [ ] **Step 3: 統合テストのREDを確認する**

Run: `php -d assert.exception=1 tests/php/test_delivery.php`

Expected: 新しい統合assertにより非0終了するのが基本。ただしTasks 2–3の実装だけで新assertも既に満たす場合は0終了でもよく、意図的に製品コードを壊してREDを作らない。いずれの場合も、追加したassertが実際に実行されたことを一時的な固定failureで一度確認してから元へ戻す。

- [ ] **Step 4: 実経路の呼出しを新しいbuild契約へ合わせる**

`test_delivery.php` 内の直接 `build()` 呼出しをv2 Date、LF末尾1個、必要な `test` 値へ変更する。本体の通常経路には再帰抑止順序の変更を加えず、`SystemMailAuthenticator::isAuthentic()` のv1/v2対応だけで通す。

- [ ] **Step 5: 新しいruntime classの明示pin検証を4か所へ追加する**

次の既存配列へ `SystemAlertFormatter.php` を追加する。

- `tests/php/test_release_validator.php` の `$runtimeDependencies`
- `tests/php/test_validate_release_entrypoint.php` のruntime dependency配列
- `tests/python/test_release_workflow.py` のsetUp copy配列
- `tests/python/test_release_workflow.py` のstable runtime assertion配列

`manager/release_deployer.py` は全PHPを自動収録し、`macos/install_app.py` はPHP `src/` をbundleへ固定列挙しないため変更しない。

- [ ] **Step 6: READMEの運用説明を更新する**

実障害/復旧/テストを件名で区別できること、利用者向け時刻がJSTであること、障害中に未通知のメールは自動再通知されずXserverメールボックスの確認が必要なことを、秘密値なしで記載する。

- [ ] **Step 7: 統合・release検証をGREENにする**

Run:

```bash
php -d assert.exception=1 tests/php/test_delivery.php
php -d assert.exception=1 tests/php/test_release_validator.php
php -d assert.exception=1 tests/php/test_validate_release_entrypoint.php
python3 -m unittest discover -s tests/python -p 'test_release_workflow.py' -v
```

Expected: 全コマンド0終了、各PHP testはPASS、Pythonは `OK`。

- [ ] **Step 8: Task 4をコミットする**

```bash
git add tests/php/test_delivery.php tests/php/test_release_validator.php tests/php/test_validate_release_entrypoint.php tests/python/test_release_workflow.py README.md
git commit -m "test: cover Japanese alert release path"
```

---

### Task 5: 全検証、独立レビュー、本番配備を行う

**Files:**
- Verify: repository-wide tracked files
- Verify: installed Mac app
- Verify: private Xserver release outside `public_html`

- [ ] **Step 1: 全自動テストと公開情報検査を実行する**

Run:

```bash
bash tests/run-all.sh
git diff --check
git status --short
```

Expected: `PASS: all tests, syntax checks, and public secret scan`、diff check成功、意図した変更だけが表示される。

- [ ] **Step 2: 仕様トレーサビリティを自己レビューする**

設計書の「件名と本文」「原因表示」「JST」「v1/v2」「状態遷移」「プライバシー」各項目をテスト名へ1対1で対応付ける。`TODO`、`TBD`、`PLACEHOLDER`、実環境識別子を検索し、0件を確認する。

Run:

```bash
rg -n "TODO|TBD|PLACEHOLDER|UTC time:|Message-ID hash:|Operational log:" src tests README.md
```

Expected: 新規実装・文面に未完了印や旧英語本文が0件。互換fixture/test helper内の意図した旧v1表現だけは、該当テストの根拠を確認して残す。

- [ ] **Step 3: サブエージェントへ実装レビューを依頼する**

実装担当とは別のレビュー担当に、設計適合、v1互換、MIME canonical性、状態遷移、privacy、release packagingを確認させる。Critical/Importantが1件でもあれば修正してfocused testと `bash tests/run-all.sh` を再実行する。Critical/Importantが0件になるまで完了扱いにしない。

- [ ] **Step 4: Mac管理アプリを既存installerで更新・検証する**

Run: `./macos/install_app.command`

Expected: installerが正常終了し、署名・bundle allowlist・同梱manager検証が成功する。アプリの「同期診断」で秘密設定を表示せず、remote configとactive releaseを読み込めることを確認する。

- [ ] **Step 5: 配備前の切戻し情報と停止条件を固定する**

「同期診断」が示す現在のactive release IDと、それに対応する公開Git commit SHAを作業記録へ残す。そのcommitをcleanな一時worktreeへcheckoutし、`bash tests/run-all.sh` が通ることを確認して、Mac管理アプリの「新リリースを検証・配備」で再配備できるrollback sourceとして保持する。

次のいずれかが起きたら新releaseの追加試験を直ちに停止し、rollback sourceを新しい `release-rollback-` IDで検証配備してactiveへ切り戻す。

- stage/readback/SSH validation/locator switchの失敗。
- 日本語の障害通知または復旧通知が届かない、2通以上届く、認証に失敗する。
- 通常メールのLINE WORKS通知が止まる。
- stateが `healthy` に戻らない、またはprivacy sentinelがメールへ現れる。

切戻し後は「同期診断」でactive release、health、最新ログを再確認し、通常のLINE WORKSテストが成功するまで新releaseを再度有効化しない。期限付き強制障害設定は最大10分で失効する既存機構を使い、失敗時に無期限の強制障害を残さない。

- [ ] **Step 6: versioned private releaseとして配備する**

Mac管理アプリの「新リリースを検証・配備」を使い、このworktreeをsourceとして指定する。配備先が `public_html` 外、directory mode `0700`、file mode `0600`、entrypointだけ実行可能な既存設計どおりであることをremote readbackで確認する。

- [ ] **Step 7: 期限付き強制障害E2Eを行う**

管理アプリの既存テスト機能で、次を順番に確認する。

1. 日本語のテスト障害メールが1通届く。
2. 同一障害中の追加失敗では2通目が届かない。
3. 復旧後、日本語のテスト復旧メールが1通届く。
4. 件名が `【テスト・対応不要】...`、本文時刻がJST、hash/log path/秘密値がない。
5. remote stateは復旧後 `healthy`、UTC `changed_at`、classification/hashなしの従来schemaである。

- [ ] **Step 8: 最終コミットと公開mainと本番releaseを同一commitへ揃える**

E2Eでコード修正が発生した場合だけ追加コミットし、全検証を再実行する。公開mainが計画開始時から変わっていなければfast-forward-onlyでレビュー済みbranchをmainへ統合し、そのexact commitを配備済みreleaseと対応付ける。

mainが先行していてfast-forward-onlyにできない場合は、main上で統合したexact commitに対して `bash tests/run-all.sh` と独立レビューをやり直し、そのexact main commitを新しいversioned releaseとして再配備してStep 7のE2Eを再実行する。競合解消後のcommitを、feature branchで配備した旧releaseと同一視してはならない。

最後に次の3つをreadbackで一致確認する。

1. 公開GitHub remote mainのcommit SHA。
2. ローカルmainのcommit SHA。
3. active releaseのsourceとして記録したcommit SHA。

3つが同一で、公開secret scanと本番healthが成功した場合だけ完了とする。一致しない場合は公開または配備を完了扱いせず、同一SHAへ揃えて全検証とE2Eを再実行する。
