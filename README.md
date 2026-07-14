# Xserver Mail to LINE WORKS

Xserverで受信したメールをメールボックスへ残したまま、LINE WORKS Incoming Webhookへ即時通知するためのCLIツールです。サーバー側のPHPがMIMEメールを解析・通知し、Mac側の日本語管理CLIが通知対象、エラー通知先、Xserverメール振り分け、秘密設定を管理します。

このリポジトリにはWebhook URL、実メールアドレス、XserverのサーバーID・ホスト名、APIキー、FTPS認証情報を保存しないでください。サンプル識別子には `example.invalid` だけを使用しています。

## 要件

- Xserver上のPHP 8.1以上（CLI、cURL、mbstring）
- ローカルのComposer 2
- macOSとPython 3（管理CLIおよびmacOSキーチェーン用）
- XServer APIで対象サーバーのメール振り分けを参照・追加・削除できるAPIキー
- Explicit FTPS（TLS保護データ接続）を利用できるアカウント
- LINE WORKS Incoming Webhook
- XserverでPHP CLIをコマンド転送に使用できること

iPhone/Safari用の管理画面、添付ファイル転送、受信メールの保存機能は含みません。

## ローカル準備

```sh
composer install
cp config/config.example.json /tmp/mail-notifier-config.example.json
```

`config/config.example.json` は項目説明用です。実設定をリポジトリ内へコピーしないでください。Webhook URL、通知先、認証情報をシェル履歴やログへ直接記録する操作も避けます。

XServer APIキーとFTPS認証情報は、macOSキーチェーンの「インターネットパスワード」へ登録します。APIキーの項目はサーバー `api.xserver.ne.jp`、アカウント `Bearer`、プロトコル `https`（`security` コマンド上の値は `htps`）、ポート `443` とします。FTPSの項目は接続先ホスト、実際のFTPSログインアカウント、プロトコル `ftps`、ポート `21` を持つ1項目とし、その項目のパスワード欄へFTPSパスワードを保存します。username用とpassword用に項目を分けません。

管理CLIはAPI項目を上記4属性で検索します。FTPS項目は環境変数の接続先ホストとプロトコル・ポートで秘密を表示しないメタデータ検索を行い、アカウントが一意に決まった場合だけ、同じアカウントを指定してパスワードを読みます。0件、複数件、または不正なメタデータは拒否します。秘密値をコマンド引数、標準出力、ログ、例外へ含めません。非秘密の接続情報だけを環境変数で指定します。

```sh
export XSERVER_SERVERNAME='server.example.invalid'
export XSERVER_COMMAND_PATH='/home/example/private/current/bin/mail-to-lineworks.php'
export XSERVER_FTPS_HOST='ftp.example.invalid'
export XSERVER_CONFIG_PATH='/mail-lineworks/private/config.json'
export XSERVER_HOME='/home/example'
python3 manager/manage.py
```

`XSERVER_COMMAND_PATH`には後方互換のため空白を含まないPHPスクリプトの絶対パスだけを指定します。実際の管理対象は `XSERVER_HOME` 配下の固定private wrapperへ導出され、Xserverで実動確認した `| /usr/bin/php8.5 /absolute/mail-forward-command-701.php` の完全なbyte列を使用します。wrapperだけを `0701`、wrapperがrequireするstable bootstrapを `0700` とし、いずれも公開領域外へ配置します。`|`、PHP実行パス、引数は環境変数へ含めないでください。

APIキーには必要最小限の権限を付与し、初回操作前に対象サーバーとmail-filterのGETが許可されることを確認してください。403は権限不足、429はレート制限として扱われます。

## 公開領域外への配備

PHP本体、`vendor/`、設定、ログ、配備時の一時ファイルは、すべて `public_html` 外の専用ディレクトリへ配置します。例えば `/home/example/private/` 以下を使い、PHPと依存物は所有者だけが読める権限、実行入口は必要最小限の実行権限、`config.json` とログは `600` を基本にします。

`FtpsDeployer` はExplicit FTPS、passive mode、`PROT P`を使い、一時名へのアップロード、権限設定、renameの順で更新します。配備先や設定先に `public_html` が含まれる場合は拒否します。最初の配備前にサーバーのPHP CLIパス、必要拡張、Composer依存物の読込、sendmailの利用可否を確認してください。

秘密設定は `config/config.example.json` を参照して公開領域外に作成します。通常配送で使う `webhook_url`、1件以上のメールアドレスを持つ `error_recipients` 配列、`log_path` を設定し、テスト用強制失敗項目は通常 `null` のままにします。Webhook URLをREADME、チケット、Git差分へ貼り付けないでください。認証済みシステムメール用の `system_mail_hmac_key` は管理CLIが欠損時だけ生成して秘密設定へ保存するため、例示値を作成したり画面、ログ、チケットへ転記したりしないでください。

`notification_base_address` には転送先となる基準アドレスを設定します。配送済みメッセージの重複抑止状態を保存する `dedup_path` は絶対パスで指定し、設定やログと同様に必ず `public_html` 外（例: `/home/example/private/state/`）へ置いてください。旧設定に `dedup_path` がない場合だけ、検証済みの非公開 `log_path` と同じディレクトリの `delivery-dedup.json` を使用します（親0700、ファイル600）。明示値が相対パス、symlink、または `public_html` 内なら起動検査は失敗します。

## Xserverメール振り分け

管理CLIで変更前の既存ルールと差分を確認し、完全一致の日本語確認文字列を入力した場合だけ反映します。通知対象ごとに次の形の管理対象ルールを追加します。

- 条件: Xserverの `header` フィールドが通知対象メールアドレスの文字列を `contain`（包含）
- アクション: `| /usr/bin/php8.5 /absolute/mail-forward-command-701.php` 形式で、公開領域外の固定wrapperへコマンド転送
- 転送方式: `copy`（メール原本を通常のメールボックスへ残す）

既存の無関係なルールは変更しません。変更操作は「新ルール追加 → API readback確認 → 旧ルール削除」の順であり、完全な原子的更新ではありません。途中で失敗した場合は診断画面とXserver側のルールを照合してください。

## Mac管理CLI

`python3 manager/manage.py` から次を操作できます。

- 通知対象の一覧・複数追加・変更・削除
- エラー通知先の一覧・複数追加・変更・削除
- APIルール、リモート設定、リリース情報の同期診断
- LINE WORKS接続テスト
- 期限付き・件名トークン限定のエラーメール経路テスト
- メニュー13による、転送設定を基準にした通知対象の同期
- メニュー14によるWebhook URLの確認
- メニュー15によるWebhook URLの接続確認付き変更

### Webhook URLを確認・変更する

メニュー14はWebhook URLのtoken部分を通常は伏せて表示します。全文が必要な場合だけ、画面に表示された完全一致の確認文字列 `Webhook URLを表示する` を入力すると、その実行中に一度だけ表示します。画面共有、端末ログ、チケットへの転記に注意してください。

メニュー15では新しいURLを対話入力します。URLは `https://webhook.worksmobile.com/message/<token>` のcanonical形式だけを受け付け、固定の秘密を含まないテスト通知が厳密にHTTP 200を返した場合だけ保存へ進みます。例示する場合は `https://webhook.worksmobile.com/message/token-placeholder` のようなダミー値だけを使ってください。

秘密設定の読取りと保存は、SSHの既知接続先検証を通した所有者専用のサーバー側helperで行います。設定全体のhashを使うcompare-and-swapにより、他の変更との競合時は保存せず再試行を求めます。保存処理が安全に完了できなかった場合、helperは条件付き復元を行い、Mac側は危険な無条件rollbackを行いません。変更または変更不要という結果の後にも設定を読み直し、helperが返した新hashと入力URLの両方が一致した場合だけ完了と表示します。復元、競合、SSH読取り失敗、readback不一致では秘密を表示せず失敗します。

### 転送設定から通知対象を同期する

管理CLIが作るXserver振り分け条件は、`field` が `header`、`match_type` が `contain`、`keyword` が通知対象メールアドレスの完全な文字列です。この経路では管理CLIやPHPがTo/Ccを解析・正規化して判定するのではなく、Xserverがメールヘッダー全体にその文字列が含まれるかを判定します。そのため通常はTo/Ccに文字列が現れるメールが一致し、配送時のヘッダーにBccアドレスの文字列が現れないメールは一致しませんが、特定ヘッダーだけに限定した照合ではありません。

メニュー13は `notification_base_address` を起点にXserverの転送設定から転送元を自動検出し、現在の管理対象フィルターとの差分を表示します。既存環境の同一domain・完全アドレス・同一専用copyコマンドの旧 `to` / `match` ルールもここで自動移行されます。全対象の新 `header` / `contain` ルールを追加・読み戻した後だけ、journalに記録した旧ルールを退役するため、途中終了後もメニュー13を再実行して安全に再開できます。まず同期診断をdry-runとして実行し、追加・削除予定とリモート設定を確認してください。差分表示だけではAPIや設定を書き換えません。

反映する場合はメニュー13を選び、表示された差分を確認して、完全一致の確認文字列 `通知対象を同期する` を入力します。処理は新しいフィルターを追加してAPI readbackを確認してから古いフィルターを削除し、最後にリモート設定を更新します。確認中の競合、readback不一致、設定更新失敗では処理を停止し、可能な範囲で追加・削除をロールバックします。失敗後は同期診断を再実行し、差分が残る場合は原因を解消してから再同期してください。

外部書き込みの前に対象と差分が表示されます。確認中にリモート設定が変わった場合は競合として書き込みを停止します。診断で「不一致」となった場合は、管理対象ルールの宛先とコマンドパス、リモート設定の `notification_targets`、`command_path`、`release_path` を確認してください。

### 恒久通知対象を管理する

メニュー1〜4はprivate設定の `notification_pinned_targets` だけを一覧・追加・変更・削除します。恒久対象とXserver転送設定から自動検出した対象の和集合が `notification_targets` です。自動検出だけでも残る対象を恒久一覧から外しても振り分けルールは削除されず、自動検出対象そのものを恒久対象の削除操作で消すこともできません。

変更時は差分と完全一致の確認文字列を確認した後、mode 600のprivate journalへ意図と設定digestを先に保存します。新ルールの追加とreadback、旧管理ルールの削除とreadback、秘密設定のCAS更新、APIと設定の最終一致確認の順に進みます。中断時は同じ操作を再実行するとjournalの意図から再開します。同期診断は個別アドレスを表示せず、恒久対象がAPIへ登録済みか、API対象件数と設定対象件数が一致するかだけを表示します。

## 通知とエラー経路

通常経路は「メール標準入力 → MIME解析 → LINE WORKS Webhook → 秘密を含まないJSON Linesログ」です。通知は受信日時、From、To、Cc、Bcc、件名、本文の順で、空のCc/Bccは省略します。プレーン本文を優先し、HTMLしかない場合はテキスト化します。添付とインライン画像は送信しません。

Webhook成功時はエラーメールを送りません。通常Webhookの失敗後に秘密を含まないエラーWebhookも失敗した最初の観測で、固定引数のsendmailから `error_recipients` の全宛先へ障害メールを1通送ります。障害中の失敗は抑止し、通常またはエラーWebhookが次に成功した最初の観測で復旧メールを1通送ります。メールはHMAC認証済みの専用ヘッダーで再帰配送を抑止し、元メール本文、アドレス、Webhook URL、認証鍵、例外メッセージを含めません。

障害メールは「要確認」、復旧メールは「復旧・要確認」、管理CLIから送る動作確認メールは「テスト・対応不要」と件名で区別できます。メール本文とDateヘッダーの利用者向け時刻はJSTです。障害中にLINE WORKSへ通知されなかったメールは復旧後も自動では再通知されないため、障害発生から復旧までの新着メールをXserverメールボックスで確認してください。

状態はprivateログと同じディレクトリのmode 600 `delivery-health.json` へ単調な観測sequenceとともに保存します。並行処理では新しい観測だけを反映するため、遅れて完了した古い失敗が復旧済み状態を戻すことはありません。sendmail成功直後かつ状態commit前にプロセスが停止した場合だけ、通知欠落を避けるため次回観測で同じ遷移メールが重複する可能性があります。この境界はat-least-onceです。

429はサーバー指定時間を上限内で1回だけ再試行します。通信タイムアウトは重複防止のため再送・分割しません。明示的な400 invalid parameterかつローカルsoft cap超過の場合だけ段落単位に分割します。

## 障害対応

1. メールボックスに原本があるか確認します。なければ通知ツールではなくXserver受信・振り分け設定を調べます。
2. 管理CLIの「同期診断」で恒久対象のAPI登録数、APIと設定の対象件数、コマンドパス、リリース情報を比較し、配信状態が「未作成」「正常」「障害中」「状態ファイル不正」のどれかを確認します。診断は固定helperのredacted summaryだけを使い、アドレス、鍵、Message-ID hash、Webhook token、状態ファイルpathを表示しません。「状態ファイル不正」の場合は通知メールを再試行する前にprivate directory chain、owner、mode 600、JSON schemaを確認します。
3. 公開領域外のJSON Linesログで `outcome`、`classification`、`http_status` を確認します。ログへ本文や秘密値を追加しないでください。
4. 403ならAPI権限、429なら待機後の再操作、FTPS失敗なら証明書検証・Explicit FTPS・権限・配備先を確認します。
5. LINE WORKSテスト後、確認付きのエラーメールテストを実行し、10分以内に件名トークン付きの合成メールを1通送ります。終了後は期限付きテスト設定が無効になったことを確認します。
6. `public_html` 以下にPHP、設定、ログ、一時ファイルが存在しないことを再確認します。

公開領域の監査は、所有者が実行ユーザーでグループ・その他から書込み不能な項目だけをたどります。権限または所有者を信頼できないサブツリーや特殊項目は内容を開かずにスキップし、件数を検証結果へ残します。スキップ件数がある場合、その内部に既知ファイル名がないことまでは証明できないため、配備自体を妨げず警告として扱います。

入力上限は10 MiBです。上限超過、壊れたMIME、無効設定でもメール配送プロセスを壊さないようCLI入口は例外を外へ出さず終了します。そのため、配送障害の判断には秘密を含まない運用ログとフォールバック通知を併用してください。

## テスト

テストは実Xserver、実FTPS、実Webhook、実メールアドレスへ接続しません。一括検証はPHP/Pythonテスト、PHP/Python構文検査、Git追跡対象の秘密情報スキャンを実行します。

```sh
bash tests/run-all.sh
```

公開前にはこのコマンドを実行し、Webhookトークン、`example.invalid` 以外のメールアドレス、実環境識別子、秘密設定ファイル、実在する `public_html` 配備パスが追跡対象にないことを確認してください。
