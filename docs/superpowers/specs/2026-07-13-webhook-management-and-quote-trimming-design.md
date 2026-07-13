# Webhook管理と引用省略 設計

## 目的

既存のMac管理アプリからLINE WORKS Incoming Webhook URLを安全に確認・変更できるようにする。同時に、LINE WORKS通知のタイトルを差出人と件名に変更し、返信メールの引用履歴を通知から省略する。元メール、Xserverのメールボックス、転送されたメール原文は変更しない。

## 対象範囲

- Mac管理CLIとFinderから起動するMacアプリのメニュー
- Xserverの公開領域外にある秘密設定の読取・原子的更新
- LINE WORKS通知のタイトルと本文整形
- プレーンテキストおよびHTMLメールの引用省略
- 自動テスト、秘密情報走査、Macアプリ再インストール、本番配備と動作確認

iPhone/Safari管理画面、LINE WORKS OIDC、メール署名の除去、元メールの加工は対象外とする。

## Mac管理メニュー

既存メニューの後ろへ次を追加する。

```text
14. Webhook URL確認
15. Webhook URL変更
```

既存の`13. 通知対象を転送設定から同期`は番号と動作を変更しない。

### URL確認

通常の確認では、スキームとホストを残し、Webhook token部分を伏せ、末尾4文字だけを表示する。tokenが短い場合も全文を表示しない。

```text
https://webhook.worksmobile.com/message/********…1234
```

URL全体を確認する場合は、画面に示した日本語の確認文字列を利用者が正確に入力したときだけ、その実行中に一度だけ標準出力へ表示する。URLをファイル、ログ、例外、シェル履歴、コマンド引数へ出さない。確認をキャンセルした場合はマスク表示だけで終了する。

### URL変更

1. 新しいURLを対話入力で受け取る。コマンドライン引数には取らない。
2. URLを出力せず、`https`、ホスト`webhook.worksmobile.com`、標準443番、`/message/<単一token>`、userinfo・query・fragmentなしを検証する。
3. tokenを伏せた新旧表示と変更内容を示し、日本語の確認文字列を要求する。
4. 新URLへ秘密を含まない「Webhook URL変更テスト」通知を送る。
5. HTTP 200を確認できた場合だけ、Xserverの秘密設定を更新する。
6. 更新後の設定を読み戻し、入力値と一致することを秘密値非表示で確認する。
7. いずれかが失敗した場合は旧設定を維持し、秘密を含まない分類済みエラーだけを表示する。

## Xserver秘密設定へのアクセス

設定は引き続き`public_html`外、所有者専用ディレクトリ0700、設定ファイル0600とする。現在のFTPS経路は0600ファイルの読取を拒否するため、Webhook管理は既存の専用SSH aliasを使用する。

読取は固定絶対パス、固定SSH設定、非対話モード、既知ホスト検証を用いる。設定JSONは最大サイズ、通常ファイル、非symlink、所有者、mode 0600、必要schemaを検証する。

読取と更新には、リポジトリで管理し配備時にhash・owner・通常ファイル・mode 0700を検証するstandalone PHP helperを使用する。実行pathは、Macの非秘密設定から取得した`filesystem_home`が正規表現`\A/home/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z`へ完全一致することを専用validatorで確認してから、固定suffix`/private/xserver-mail-lineworks/bootstrap/manage-private-config.php`を結合して導出する。空白、制御文字、quote、`;`、`$`、backtickその他のshell metacharacterを含むhomeは拒否する。実環境固有のhomeはコードや公開文書へ固定しない。SSH remote commandは固定PHP binaryと、さらにPOSIX shell quoteした検証済み絶対helper pathの2要素だけから構築し、この正規表現境界とargv生成を攻撃文字列のnegative testで固定する。操作内容と秘密値は最大64 KiBの設定を収容できる最大128 KiBのJSON標準入力で渡す。helperの応答も最大128 KiBのJSONとし、秘密値を返すのは`read`操作だけとする。

`read`要求は`{"schema_version":1,"operation":"read"}`だけを許可する。helperは自身の検証済み固定配置から`dirname(__DIR__, 3)`でhomeを得て、固定suffix`/mail-lineworks/private/config.json`を結合するため、設定pathをremote command、引数、stdin、環境変数から受け取らない。導出したhomeも同じ厳格ASCII規則へ一致させる。helperは設定を開き、通常ファイル、非symlink、実行UID所有、mode 0600、最大64 KiB、JSON object、必須キーを検証し、設定bytesとSHA-256を暗号化SSHの標準出力へ返す。JSON escape等のoverheadを含む応答全体は128 KiBを上限とする。Mac側は値をメモリ内だけで扱う。

`compare_and_swap`要求はschema version、operation、期待する旧SHA-256、完全な新設定JSONだけを許可する。helperは単一プロセス内で現在bytesを再読込し、旧SHA-256が一致する場合だけ、同じディレクトリ内に排他的作成した推測困難な一時ファイルへ完全なJSONを書き、flush、mode 0600、owner、size、SHA-256を検証してrenameする。既存の未知キーはMac側で保持し、helperも旧設定に存在する未知キーの欠落を拒否する。置換後は再度通常ファイル・owner・mode・bytes hashを読み戻す。

置換後readbackが意図した新SHA-256と一致しない場合、helperは「現在hashが意図した新SHA-256と一致する」場合だけ、メモリに保持した旧bytesを別の一時ファイルから同じ手順で原子的に復旧する。第三者更新を検出した場合は上書きせず競合として停止する。応答は`changed`、`conflict`、`unchanged`、`restored`の状態と旧・新hashだけを返し、URLや設定値を含めない。Webhook URLをSSHコマンド文字列や引数へ埋め込まない。

## LINE WORKS通知形式

Webhook payloadの`title`は、解析済みの差出人と件名を使って次の形式にする。

```text
差出人：件名
```

差出人が空の場合は`（差出人不明）`、件名が空の場合は`（件名なし）`を使う。改行・制御文字を除去し、結合後のタイトルをUnicode code point単位で先頭100文字までに切り詰める。不正UTF-8は既存の置換文字処理を通し、byte途中で切らない。本文の並びは次で固定する。

```text
受信日時
From
To
Cc（ある場合）
Bcc（ある場合）
添付ファイル（ある場合）
件名
本文
```

Cc、Bcc、添付ファイルは存在しない場合、見出しも表示しない。添付内容は送らず、既存どおり安全化したファイル名とサイズだけを表示する。

## 引用省略

引用省略はLINE WORKSへ送る表示本文にだけ適用する。`MailMessage`は正規化済みの全文`body`と通知専用の`notificationBody`を別々に保持し、`NotificationFormatter`は後者だけを使う。保存メールは変更せず、将来の処理が全文を必要とする場合は`body`を利用できる。

### HTML

HTMLから`body`を作る既存経路は維持する。別の通知用経路では、本文をtext化する前に`blockquote`要素と、class属性の空白区切りtokenにASCII大小文字を無視して`gmail_quote`を持つ要素を除外する。部分一致（例：`not_gmail_quote`）は除外しない。script/style除去など既存の安全化を維持する。DOM拡張の有無に依存せず、壊れたHTMLでも処理時間と入力サイズが有界になる実装とする。

### プレーンテキスト

次を引用開始または引用行として認識する。

- `>`で始まる引用行
- `On ... wrote:`および日本語の同等な返信開始行
- `-----Original Message-----`
- Outlook型の過去メール見出し（6行以内に「差出人」と「送信日時」を必須とし、「宛先」「件名」の一方以上を含むまとまり）
- 「以下のメッセージを引用」などの明示的な引用開始行

明示的な引用開始境界以降は省略する。`On ... wrote:`は同一行で`On`から始まり`wrote:`で終わる場合、日本語境界は返信日時と送信者を含む定型境界の場合だけ認識する。単独の`>`引用行は削除するが、その前後にある新規本文は残す。通常本文中に偶然現れる「件名」「From」「差出人」「送信日時」など単独の語や、成立条件に満たない並びでは引用開始と判定しない。署名は削除しない。

引用除去後に空白しか残らない場合、本文欄へ次を表示する。

```text
（引用部分は省略しました）
```

## エラー処理と秘密保持

- Webhook URL、token、APIキー、FTPS/SSH認証情報をGit、ログ、テストfixture、例外、配備成果物へ含めない。
- URL変更テストのpayloadにも旧URL・新URLを含めない。
- URL確認の全文表示は利用者が明示確認した場合だけであり、その値をアプリ内に保持しない。
- 接続失敗、HTTPエラー、競合、readback不一致では設定を変更しない。
- 原子的置換後のreadbackが一致しない場合は明確に失敗として扱い、可能な場合は検証済み旧bytesへ原子的に復旧する。
- `public_html`境界監査と所有者専用権限を維持する。

## テスト

- URLマスクがtoken全文を含まず、短いtokenでも漏えいしない。
- 正しい確認文字列の場合だけ全文表示し、キャンセル時は表示しない。
- 不正scheme、host、port、path、userinfo、query、fragmentをネットワーク通信前に拒否する。
- 新URL変更テストはHTTP statusがexact 200の場合だけ成功とし、201や204を含む他のstatusでは設定を更新しない。既存の通常通知が採用する2xx判定は変更しない。
- timeout、HTTPエラー、SSH失敗、競合、readback不一致では旧設定を維持する。
- 設定の未知キー保持、mode 0600、非symlink、最大サイズ、原子的renameを検証する。
- タイトルが`差出人：件名`となり、空値の具体的代替表示、改行・制御文字除去、100 Unicode code pointの境界を満たす。
- Cc、Bcc、添付の有無と表示順を検証する。
- 通常本文、`>`引用、Gmail、Outlook、Original Message、日本語境界、HTML blockquoteを検証する。`not_gmail_quote`、単独のFrom/件名/差出人、Outlook成立条件未満、通常文中の`wrote:`は削除しないnegative fixtureを含める。
- 引用の前後にある新規本文を誤って削除しない。
- 引用だけのメールでは省略表示になる。
- 全Python/PHPテスト、構文検査、秘密情報走査、生成Macアプリ走査を実行する。

## 完了条件

- MacアプリからWebhook URLのマスク確認、明示的全文確認、検証付き変更ができる。
- 変更失敗時に旧URLが維持され、成功時は新URLのHTTP 200とXserver readbackを確認できる。
- LINE WORKS通知タイトルが差出人と件名になり、本文から引用履歴が省略される。
- 添付ファイル表示がBccの後、件名の前になる。
- 本番ファイルは`public_html`外、ディレクトリ0700、通常ファイル0600、コマンドwrapper701を維持する。
- GitHub公開物とログに実Webhook URLその他の秘密が存在しない。
