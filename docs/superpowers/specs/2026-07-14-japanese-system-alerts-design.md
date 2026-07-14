# 障害・復旧メール日本語化設計

## 目的

Xserverメール通知システムが送る障害通知・復旧通知を、日本語利用者が迷わず判断できる文面へ変更する。表示日時は日本時間とし、実障害、復旧、動作確認を件名だけで区別できるようにする。

元メールの差出人、宛先、件名、本文、添付ファイル名、Webhook URL、認証鍵、例外文は引き続き通知メールへ含めない。障害中にLINE WORKSへ通知されなかったメールは自動再通知されないため、復旧メールでもXserverメールボックスの確認を案内する。

## 採用方針

「行動優先の日本語本文＋末尾に最小限の管理情報」を採用する。

- 冒頭で、発生した事象と必要な対応を日本語で説明する。
- 日時は `yyyy年MM月dd日（EEE）HH時mm分ss秒` の日本時間で表示する。`EEE` は `日・月・火・水・木・金・土` の日本語1文字曜日とする。
- 原因は日本語説明を先に表示し、allowlist済みの内部原因コードを管理者向け情報へ残す。
- 元メールのMessage-IDに由来する64桁識別子はメール本文へ表示しない。内部状態とログだけに保持する。
- 元メールに由来しないランダムevent IDは認証用カスタムヘッダーだけに保持し、通常の本文へ表示しない。
- サーバー内ログの絶対パスはメール本文から削除する。
- 内部状態とJSON Linesログの時刻はUTCのまま維持する。

## 件名と本文

### 実障害

件名：`【要確認】LINE WORKSメール通知で障害が発生しました`

```text
LINE WORKSへのメール通知で障害が発生しました。
復旧するまで、LINE WORKSへ通知されない可能性があります。

【必要な対応】
Xserverのメールボックスで新着メールを直接確認してください。
このメールへの返信は不要です。

障害発生日時：2026年07月14日（火）17時33分55秒
障害内容：LINE WORKSに接続できませんでした。

【管理者向け情報】
原因コード：transport_error
確認方法：Macの「Xserverメール通知管理」アプリで「同期診断」を実行してください。
```

### 実障害からの復旧

件名：`【復旧・要確認】LINE WORKSメール通知が復旧しました`

```text
LINE WORKSへのメール通知は復旧しました。
今後受信する対象メールは通常どおり通知されます。

障害中にLINE WORKSへ通知されなかったメールは自動では再通知されません。

【必要な対応】
障害発生日時から復旧日時までの新着メールを、
Xserverのメールボックスで確認してください。
このメールへの返信は不要です。

復旧日時：2026年07月14日（火）17時35分10秒
障害発生日時：2026年07月14日（火）17時33分55秒
障害内容：LINE WORKSに接続できませんでした。
現在の状態：正常

【管理者向け情報】
原因コード：transport_error
```

### 障害通知の動作確認

`forced_test_failure` は実障害として表示しない。

件名：`【テスト・対応不要】障害通知メールの動作確認`

```text
これは管理者による障害通知メールの動作確認です。
実際の障害ではありません。対応は不要です。

テスト実行日時：2026年07月14日（火）17時33分55秒
確認結果：障害通知メールを正常に送信しました。

【管理者向け情報】
原因コード：forced_test_failure
```

### 動作確認後の復旧

件名：`【テスト・対応不要】復旧通知メールの動作確認`

```text
これは管理者による復旧通知メールの動作確認です。
実際の障害ではありません。対応は不要です。

テスト実行日時：2026年07月14日（火）17時35分10秒
確認結果：復旧通知メールを正常に送信しました。

【管理者向け情報】
原因コード：forced_test_failure
```

## 障害内容の日本語対応

内部コードはログや状態との照合用に変更せず、表示だけを次のallowlistで日本語化する。

| 原因コード | 表示 |
| --- | --- |
| `transport_error` | LINE WORKSに接続できませんでした。 |
| `http_error` | LINE WORKSからエラーが返されました。 |
| `rate_limited` | LINE WORKSの送信回数制限に達しました。 |
| `invalid_webhook_url` | Webhook URLの設定が正しくありません。 |
| `invalid_parameter` | 通知内容の設定に問題があります。 |
| `missing_parameter` | 通知に必要な情報が不足しています。 |
| `invalid_payload` | 受信メールを通知用に処理できませんでした。 |
| `internal_error` | メール通知処理で内部エラーが発生しました。 |
| `health_state_failure` | 障害状態を記録できませんでした。 |
| `forced_test_failure` | 障害通知メールの動作確認です。 |
| `unknown` | 原因不明のメール通知エラーです。 |

`success` と `system_mail_suppressed` は障害原因ではないため、`recordFailure` の入力として受理しない。これらまたはallowlist外の値が障害記録へ渡された場合は、メールや状態へその値を出さず `unknown` として扱う。

## 日本時間表示

- 表示用の時刻だけを `Asia/Tokyo` へ変換する。
- 曜日はロケールへ依存せず、`日・月・火・水・木・金・土` の固定配列で変換する。
- 本文は24時間表記とする。
- v2メールの `Date` ヘッダーも `+0900` とし、本文とメールアプリの表示を一致させる。
- `delivery-health.json` の `changed_at` と運用ログは既存どおりUTCで保存する。

## 認証形式v2と後方互換

日本語通知は認証形式v2として新設する。旧形式の定数を単純置換しない。

- builderはv2だけを新規発行する。
- verifierは旧v1英語形式と新v2日本語形式を両方認証し、どちらも通常メール処理より前に再帰抑止する。
- v1は英語件名、UTC `Date`、既存HMAC framingを変更しない。
- v2の件名はRFC 2047 UTF-8 Base64 encoded-wordでcanonicalに生成する。UTF-8コードポイントを分割せず最大45byteずつに区切り、各chunkを `=?UTF-8?B?<Base64>?=` として1個のencoded-wordを75文字以内に収め、複数wordをASCII空白1個で連結する。Subjectヘッダー自体は折り返さない。
- v2のtypeとcanonical件名の許可ペアは次に限定する。任意のcanonical Subjectは受理しない。
  - `error` → `【要確認】LINE WORKSメール通知で障害が発生しました` または `【テスト・対応不要】障害通知メールの動作確認`
  - `recovery` → `【復旧・要確認】LINE WORKSメール通知が復旧しました` または `【テスト・対応不要】復旧通知メールの動作確認`
- verifierはtypeに対応する上記encoded Subjectのいずれかであり、かつHMACが真正な場合だけ件名を受理する。classificationを本文から構文解析して認証判断には使わない。本文テンプレートの完全一致はbuilderとhealth monitorのテストで保証する。
- v2は次のMIMEヘッダーをexactly-onceで付ける。
  - `MIME-Version: 1.0`
  - `Content-Type: text/plain; charset=UTF-8`
  - `Content-Transfer-Encoding: base64`
- v2のdecoded本文は有効なUTF-8で、NULとCRを含まず、改行はLFだけ、末尾LFはちょうど1個とする。Unicode正規化は行わず、入力byte列をそのまま認証対象にする。
- v2本文はdecoded本文をBase64化し、76文字ごとにCRLFで折り返し、末尾をCRLF1個で終えるcanonical表現とする。verifierはstrict Base64 decode後のbyte列を変換せずSHA-256し、同じbyte列をcanonical再Base64化した結果がwire bodyと完全一致することを要求する。
- HMAC framingは既存と同じく、各fieldを `<10進byte長>:<field>\n` で連結する。v2のfield値を次の順序に固定する。ヘッダー名や `: ` は含めず、ヘッダー値だけを使う。
  1. `2`
  2. type (`error` または `recovery`)
  3. event ID
  4. canonical To値
  5. canonical encoded Subject値
  6. canonical Date値
  7. `1.0`
  8. `text/plain; charset=UTF-8`
  9. `base64`
  10. decoded本文のSHA-256小文字16進値
- verifierはヘッダーの欠落、重複、折返し、別charset、別CTE、非canonical Base64、本文hash不一致、HMAC不一致を拒否する。
- `X-Xserver-Mail-Notifier-Type` の `error` / `recovery` は機械判定値として英語のまま維持する。

### verifierのversion分岐

1. 最大header長までを読み、共通する必須ヘッダーを重複なしで構文解析する。
2. `X-Xserver-Mail-Notifier-Version` がexact `1` なら、MIMEヘッダーを要求せず、現行v1の英語件名、UTC Date、HMAC framingをbyte互換のまま検証する。
3. versionがexact `2` なら、v2専用の3つのMIMEヘッダーをexactly-onceで要求し、上記件名ペア、JST Date、canonical Base64、v2 HMAC framingを検証する。
4. versionの欠落、重複、`1` / `2` 以外は拒否する。

v2専用MIMEヘッダーをv1と共通の必須集合へ追加してはならない。これによりMIMEヘッダーを持たない真正なv1メールの再帰抑止を維持する。

この互換性により、配備中に旧リリースが生成した真正なv1メールが戻ってきても、未認証の通常メールとしてLINE WORKSへ再通知されない。

## 状態遷移

- 健全状態から最初の失敗へ遷移するときだけ、障害通知または障害通知テストを1通送る。
- 障害中の追加失敗では通知メールを送らない。
- 障害状態から最初の成功へ遷移するときだけ、復旧通知または復旧通知テストを1通送る。
- 復旧メールは状態に保持されている障害発生時刻と原因コードを使う。元メール由来の識別子は内部状態・ログ照合だけに保持し、メール本文には出さない。状態スキーマは変更しない。
- メール送信成功後にだけ状態遷移を確定する既存のfail-open境界を維持する。

## テスト

実装時に少なくとも次を自動検証する。

1. v2日本語メールのbuild→authenticate roundtrip。
2. v1真正メールを新verifierが引き続き認証し、偽造v1を拒否すること。
3. 日本語件名、Date、MIMEヘッダー、本文、Base64、HMACの各改ざん拒否。
4. 日本時間の日付跨ぎ、月跨ぎ、年跨ぎ、うるう年、曜日表示。
5. 全障害原因コードと `unknown` の日本語表示、および `success`、`system_mail_suppressed`、allowlist外値が `unknown` へ安全に縮退すること。
6. 実障害、実復旧、テスト障害、テスト復旧の件名と本文完全一致。
7. 障害1回、障害中の重複抑止、復旧1回、復旧後の重複抑止。
8. 生成wire全体に、元メールのFrom、To、Cc、Bcc、件名、本文、添付、例外、Webhook URL、鍵、ログ絶対パスが含まれないこと。Toヘッダーは設定済みエラー通知先のsentinelだけを許可する。
9. 64KiB上限、CRLF正規化、RFC 2047の行長上限。
10. 全PHP/Pythonテスト、Macアプリ検証、公開秘密情報スキャン。

本番配備後は期限付き強制障害テストを使い、実メールで日本語の障害通知・重複抑止・日本語の復旧通知を確認する。

## 対象外

- 障害中に通知されなかったLINE WORKSメッセージの自動再送。
- HTMLメール化、ロゴ、装飾、添付ファイル。
- iPhone/Safari管理画面。
- UTCで保持している内部状態・ログの移行。
