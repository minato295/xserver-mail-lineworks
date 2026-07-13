# Xserver Mail to LINE WORKS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xserver受信メールの本文全文をLINE WORKSへ即時通知し、Macから通知対象・エラー通知先・配備状態を安全に管理できる公開可能なツールを構築する。

**Architecture:** Xserverの公開領域外でPHP 8.1+ CLIがメール原文を解析し、LINE WORKS Incoming Webhookへ原則1メッセージで送る。MacのPython 3管理CLIがmacOSキーチェーンのAPIキー・FTPS認証情報を使い、XServer APIのメール振り分けルールと、FTPS上の秘密設定・配備ファイルを管理する。

**Tech Stack:** PHP 8.1+、Composer、`zbateson/mail-mime-parser:^4.0`、PHP cURL、`/usr/sbin/sendmail`、Python 3標準ライブラリ、macOS `security`、XServer REST API、FTPS Explicit、`unittest`

## Global Constraints

- GitHub公開物に実メールアドレス、実ドメイン、サーバーID、ホスト名、Webhook URL、APIキー、FTP認証情報を含めない。
- PHP、依存パッケージ、秘密設定、ログはすべて `public_html` 外へ配置する。
- メール原本はXserverのメール振り分け `action.method=copy` でメールボックスにも残す。
- 通知表示順は `受信日時 → From → To → Cc → Bcc → 件名 → 本文` とする。
- 受信日時は `Asia/Tokyo` の `yyyy年MM月dd日（EEE）HH時mm分ss秒`、曜日は日本語1文字とする。
- Cc/Bccが空または取得不能なら行を表示せず、Bccを推測しない。
- 添付・インライン画像を送らず、プレーン本文またはHTMLをテキスト化した本文全文を送る。
- LINE WORKSへは原則1メッセージで送り、明示的な400拒否かつローカル検証済み・ソフト上限超過の場合だけ分割する。タイムアウト時は重複回避のため自動分割しない。
- LINE WORKSへ障害通知できない場合だけ `/usr/sbin/sendmail` でエラーメールを送る。
- LINE WORKS成功時はエラーメールを送らない。本文・秘密値をログやエラーメールへ含めない。
- Mac管理CLIから通知対象とエラー通知先を複数、一覧・追加・変更・削除できる。
- XServer APIの既存無関係ルールを変更しない。変更は新規ルール確認後に旧ルールを削除する非原子的置換として扱う。
- 外部書き込み前に差分と対象を表示し、利用者確認後に実行する。自動テストは実サービスへ通信しない。

---

## File Structure

- `composer.json`, `composer.lock` — PHP 8.1+とMIMEパーサー依存
- `src/MailMessage.php` — 正規化済みメール値オブジェクト
- `src/MailParser.php` — MIME解析、本文選択、HTMLテキスト化、入力制限
- `src/NotificationFormatter.php` — 指定順序・日本語日時で通知本文生成
- `src/WebhookClient.php` — LINE WORKS JSON POST、レート/エラー分類、限定分割
- `src/ErrorReporter.php` — LINE WORKS障害通知とsendmailフォールバック
- `src/OperationalLogger.php` — 秘密値を含まない構造化ログ
- `bin/mail-to-lineworks.php` — Xserverメール振り分けから呼ばれるCLI入口
- `config/config.example.php` — 秘密値なしの設定例
- `manager/xserver_mail_manager.py` — Mac日本語対話式管理CLI
- `manager/xserver_api.py` — XServer APIクライアント
- `manager/ftps_deployer.py` — FTPS配備・秘密設定の原子的更新
- `tests/php/*.php`, `tests/fixtures/*.eml` — PHP単体・結合テストと架空メール
- `tests/python/*.py` — API/FTPS/管理CLIのモックテスト
- `README.md` — 公開向け導入・運用・セキュリティ説明

### Task 1: MIME解析と通知フォーマット

**Files:**
- Create: `composer.json`
- Create: `composer.lock`
- Create: `src/MailMessage.php`
- Create: `src/MailParser.php`
- Create: `src/NotificationFormatter.php`
- Create: `tests/php/test_mail_parser.php`
- Create: `tests/fixtures/plain.eml`
- Create: `tests/fixtures/html.eml`
- Create: `tests/fixtures/multipart.eml`

**Interfaces:**
- Produces: `MailParser::parse(string $raw, DateTimeImmutable $fallback): MailMessage`
- Produces: `NotificationFormatter::format(MailMessage $message): string`
- `MailMessage` properties: `receivedAt`, `from`, `to`, `cc`, `bcc`, `subject`, `body`, `messageIdHash`

- [ ] **Step 1: Composer依存と失敗テストを追加する**

`composer.json` はPHP `>=8.1`、`zbateson/mail-mime-parser:^4.0`、PSR-4 `XserverMail\\` → `src/` を定義する。テストは日本語MIMEヘッダー、複数To/Cc、Bcc有無、Dateの日本時間変換、plain優先、HTML fallback、添付除外、10MiB超過拒否を検査する。

```php
$message = $parser->parse(file_get_contents($fixture), new DateTimeImmutable('2026-07-11T00:00:00+09:00'));
assert($message->subject === 'お問い合わせ');
assert($message->body === "本文です。\n2行目です。");
assert(!str_contains($message->body, 'ATTACHMENT_SECRET'));
```

- [ ] **Step 2: REDを確認する**

Run: `php -d assert.exception=1 tests/php/test_mail_parser.php`

Expected: `MailParser`未定義またはautoload不在でFAIL。

- [ ] **Step 3: MIME解析と値オブジェクトを実装する**

`MailMimeParser()->parse($raw, false)`、`getHeader($name)?->getDecodedValue()`、`getTextContent(0, 'UTF-8')`、`getHtmlContent(0, 'UTF-8')` を使う。plainを優先し、HTMLは改行要素を改行へ変換後、許可タグなしの`strip_tags`と`html_entity_decode`でテキスト化する。Dateは`DateHeader::getDateTime()`を試し、失敗時だけfallbackを使う。Message-IDは`hash('sha256', value)`とする。

- [ ] **Step 4: 指定フォーマッターを実装する**

ヘッダー順を固定し、空Cc/Bccを省略する。曜日配列 `['日','月','火','水','木','金','土']` を使い、例 `2026年07月11日（土）16時05分09秒` を生成する。制御文字は改行・タブを除いて除去する。

- [ ] **Step 5: GREENと構文検査を確認する**

Run: `php -l src/MailParser.php && php -d assert.exception=1 tests/php/test_mail_parser.php`

Expected: `PASS: mail parser and formatter`。

- [ ] **Step 6: コミットする**

```bash
git add composer.json composer.lock src tests/php tests/fixtures
git commit -m "feat: parse and format inbound mail"
```

### Task 2: Webhook送信・エラーフォールバック・CLI入口

**Files:**
- Create: `src/WebhookClient.php`
- Create: `src/ErrorReporter.php`
- Create: `src/OperationalLogger.php`
- Create: `bin/mail-to-lineworks.php`
- Create: `config/config.example.php`
- Create: `tests/php/test_delivery.php`

**Interfaces:**
- Consumes: Task 1の`MailParser`, `NotificationFormatter`
- Produces: `WebhookClient::send(string $title, string $text): WebhookResult`
- Produces: `ErrorReporter::report(Throwable $error, string $messageIdHash): void`
- Entry: `php bin/mail-to-lineworks.php < message.eml`

- [ ] **Step 1: 送信状態遷移の失敗テストを書く**

注入したHTTP transportとsendmail transportで、HTTP 200成功、429同一payload再試行、400 missing/invalid URL非分割、400 invalid parameterかつsoft cap超過だけ分割、timeout非分割、LINE成功時メールなし、Webhook失敗時だけメール、ログ秘密値非包含を検査する。

```php
$result = $client->send('受信メール', $longText);
assert($result->isSuccess());
assert($fakeHttp->requestCount() === 1);
assert($fakeSendmail->messages() === []);
```

- [ ] **Step 2: REDを確認する**

Run: `php -d assert.exception=1 tests/php/test_delivery.php`

Expected: `WebhookClient`未定義でFAIL。

- [ ] **Step 3: Webhookクライアントを実装する**

JSONは `{"title":...,"body":{"text":...}}`。HTTP 200かつdocumented success bodyを成功とする。接続5秒・総15秒、TLS検証有効。429は`RateLimit-Reset`範囲で1回だけ同一payload再試行。timeout/通信不明は重複回避で再送・分割しない。分割soft capは設定可能で、400 invalid parameterかつsoft cap超過時だけ段落境界で分割する。

- [ ] **Step 4: ErrorReporterとログを実装する**

内部エラーは秘密なしLINE通知を先に試し、Webhook失敗時のみsendmailへ切り替える。sendmailは引数固定の `/usr/sbin/sendmail -t -i` をプロセスとして開き、ヘッダー注入を除去した宛先だけを使う。専用 `X-Xserver-Mail-Notifier-Error: 1` を付ける。ログはJSON Linesで日時、成功/失敗、Message-ID hash、分類、HTTP statusだけを記録する。

- [ ] **Step 5: CLI入口と設定例を実装する**

標準入力を10MiB+1byteまで読み、超過拒否。設定は`MAIL_NOTIFIER_CONFIG`または隣接する公開領域外パスから読み、URL・エラー宛先・ログパスを検証する。全Throwableを捕捉し、受信配送を壊さない終了コード0とする一方、設定/起動テストモードでは非ゼロを返す。

- [ ] **Step 6: GREENを確認してコミットする**

Run: `php -l bin/mail-to-lineworks.php && php -d assert.exception=1 tests/php/test_delivery.php`

Expected: `PASS: delivery and fallback`。

```bash
git add src bin config tests/php/test_delivery.php
git commit -m "feat: deliver mail notifications with fallback"
```

### Task 3: XServer API・FTPSクライアント

**Files:**
- Create: `manager/xserver_api.py`
- Create: `manager/ftps_deployer.py`
- Create: `manager/keychain.py`
- Create: `tests/python/test_xserver_api.py`
- Create: `tests/python/test_ftps_deployer.py`

**Interfaces:**
- Produces: `XServerApi.list_filters(domain=None)`, `add_filter(rule)`, `delete_filter(id)`
- Produces: `XServerApi.replace_managed_filter(old_id, new_rule)` with add→readback→delete
- Produces: `FtpsDeployer.deploy_release(local_dir, remote_root)`, `update_private_config(config)`
- Produces: `Keychain.read_api_key()`, `read_ftps_credentials()` without printing secrets

- [ ] **Step 1: APIとFTPSの失敗テストを書く**

`urllib` transportを偽装し、Bearer、HTTPS固定、list/add/delete、403権限、429、管理対象タグ識別、置換時add成功/readback成功後だけdeleteを検査する。`ftplib.FTP_TLS`を偽装し、`AUTH TLS`/`PROT P`、`public_html`を含むremote_root拒否、テンポラリ名アップロード→権限設定→renameを検査する。

- [ ] **Step 2: REDを確認する**

Run: `python3 -m unittest discover -s tests/python -v`

Expected: import失敗。

- [ ] **Step 3: キーチェーンとAPIクライアントを実装する**

`security find-internet-password ... -w`を`subprocess.run(capture_output=True)`で呼び、秘密値は返すだけでログ・例外へ含めない。APIは `https://api.xserver.ne.jp/v1/server/{servername}/mail-filter` 固定。ルールはdomain、`to`+full address+`match`、`mail_address`、command target、`copy`。管理対象は固定commentがAPIにないため、command targetの専用絶対パスと条件形で識別する。

- [ ] **Step 4: FTPS配備を実装する**

`FTP_TLS`、証明書検証既定、passive、`prot_p()`を使用する。公開領域外remote rootだけを許可。秘密設定はPHP配列を安全に生成せず、JSON設定としてアップロードし、サーバーPHPがJSONを読む。ローカル一時ファイルを作らず`io.BytesIO`からuploadする。

- [ ] **Step 5: GREENを確認してコミットする**

Run: `python3 -m unittest discover -s tests/python -v`

Expected: 全テストOK。

```bash
git add manager tests/python
git commit -m "feat: manage Xserver API and FTPS deployment"
```

### Task 4: Mac日本語対話式管理CLI

**Files:**
- Create: `manager/manage.py`
- Create: `tests/python/test_manager.py`

**Interfaces:**
- Consumes: Task 3のAPI、FTPS、Keychain
- Produces: `python3 manager/manage.py` 日本語メニュー

- [ ] **Step 1: メニューと安全確認の失敗テストを書く**

一覧、追加、変更、削除、複数登録、エラー宛先管理、同期診断、テスト通知を偽API/FTPSで検査する。削除・置換は差分表示後の完全一致確認なしでは実行しない。メール形式は`@`を含むだけでなく標準ライブラリで構文検証し、改行を拒否する。

- [ ] **Step 2: REDを確認する**

Run: `python3 -m unittest tests.python.test_manager -v`

Expected: `manager.manage` import失敗。

- [ ] **Step 3: 対話式CLIを実装する**

メニューは通知対象一覧/追加/変更/削除、エラー通知先一覧/追加/変更/削除、同期診断、LINE WORKSテスト、エラーメールテスト、終了。実アドレスをコマンドライン引数に取らず、`input()`で取得する。削除・置換・配備は対象と差分を表示し、日本語確認文字列を要求する。

- [ ] **Step 4: GREENと全Pythonテストを確認する**

Run: `python3 -m unittest discover -s tests/python -v`

Expected: 全テストOK。

- [ ] **Step 5: コミットする**

```bash
git add manager/manage.py tests/python/test_manager.py
git commit -m "feat: add Japanese Mac management CLI"
```

### Task 5: 公開README・統合検証・秘密情報スキャン

**Files:**
- Create: `README.md`
- Modify: `.gitignore`
- Create: `tests/run-all.sh`

**Interfaces:**
- Produces: 公開可能な導入・管理・障害対応手順
- Produces: `bash tests/run-all.sh` 一括検証

- [ ] **Step 1: 公開物検査を失敗テストとして追加する**

`tests/run-all.sh`はPHP/Pythonテスト、構文検査、Git追跡ファイルのWebhook URL・実在形式のfixture外メール・既知環境識別子・秘密設定ファイル・`public_html`配備パスを検査する。

- [ ] **Step 2: READMEとgitignoreを実装する**

READMEは要件、PHP 8.1+、Composer、Mac管理CLI、Xserver API権限、FTPS、公開領域外配備、設定変更、コピー転送、エラー経路、制限、テストを説明する。例は`example.invalid`のみ。`.gitignore`はvendor、config.json、log、eml、research、deploy stagingを除外する。

- [ ] **Step 3: 全検証を実行する**

Run: `bash tests/run-all.sh`

Expected: PHP/Python/秘密スキャンすべてPASS。

- [ ] **Step 4: コミットする**

```bash
git add README.md .gitignore tests/run-all.sh
git commit -m "docs: add secure deployment and operations guide"
```

### Task 6: Xserver配備・設定・実サービス検証

**Files:**
- No public repository secret changes
- Remote only: public_html外のrelease、`config.json`、log directory

**Interfaces:**
- Consumes: MacキーチェーンのAPIキー・FTPS認証情報、利用者が会話で指定した実Webhook URL・初期メールアドレス
- Produces: 配備済みPHP、XServer mail-filter copy rule、LINE WORKS通知、sendmailフォールバック

- [ ] **Step 1: 現状を読み取り専用で監査する**

APIキーの `/v1/me` で権限、server name、mail-filter GETを確認する。FTPSでremote rootと`public_html`境界を確認する。既存ルールと衝突する場合は変更せず停止する。

- [ ] **Step 2: 公開領域外へ配備する**

管理CLIでreleaseとvendorをアップロードし、秘密設定へWebhook URLと初期エラー宛先を登録する。権限を最小化し、公開URLから取得不能であることを確認する。

- [ ] **Step 3: メール振り分け実行前のローカル実Webhookを検証する**

Mac上で同一PHPコードと架空合成メールを使い、実Webhookへ指定順・全文・Cc/Bcc省略・日本語日時のテスト通知を送る。本文とタイトルには`テスト`と明記する。Webhook URLは環境変数から渡し、コマンド出力・履歴・テストfixtureへ保存しない。

- [ ] **Step 4: APIでコピー転送ルールを追加する**

差分を確認し、対象アドレスの`to` exact match、command target、method `copy`をPOSTする。GET readbackでID・条件・actionを確認する。既存無関係ルールは変更しない。

- [ ] **Step 5: ローカル状態遷移とサーバー実行条件を検証する**

ローカルの偽Webhook・偽sendmailで、Webhook失敗時だけメール、LINE成功時はメールなし、専用ヘッダーで再帰しない状態遷移を再確認する。サーバー上の実sendmail到達は次のエンドツーエンド受信でWebhook失敗テスト用の一時設定を使って確認し、確認直後に正しいWebhook設定へ戻す。

- [ ] **Step 6: エンドツーエンド受信を確認する**

設定対象へ利用者が外部メールからテストメールを送る。通常設定でメールボックスに原本が残ること、LINE WORKSへ本文全文が原則1メッセージで届くことを確認する。次に管理CLIの確認付き診断操作でWebhook失敗用の一時設定へ切り替え、2通目のテストメールで実sendmail通知を確認し、必ず正常設定へ復元する。ログに本文・秘密値がないこと、`public_html`配下に成果物がないことを確認する。
