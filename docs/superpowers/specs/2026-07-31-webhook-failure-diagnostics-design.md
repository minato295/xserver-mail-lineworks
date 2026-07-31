# LINE WORKS Webhook障害診断ログ設計

## 目的

LINE WORKS Incoming WebhookがHTTP 500などを返した際に、メール本文や認証情報を保存せず、原因調査に必要なプロバイダー応答と再試行経過を本番環境で確認できるようにする。

## 保存場所と権限

既存の運用ログを使用する。

`/home/s3710/mail-lineworks/private/log/mail-notifier.jsonl`

- `public_html`配下へは保存しない。
- ログファイルは所有者のみ読み書き可能な`0600`とする。
- 親ディレクトリは所有者のみアクセス可能な`0700`とする。
- サーバー実行時は通常ファイルであること、実効UID所有者、権限、許可されたprivate領域内のパスであることを検証する。
- Mac管理CLIはXserver本人の認証済みFTPS接続、本人ホーム配下の固定private領域、通常ファイル、`0600`を検証する。FTPSのMLSTが所有者名を返さない場合も、認証済みアカウント境界を所有者境界として扱う。
- 権限またはファイル種別が不正な場合は診断情報を書かず、安全側に停止する。メール処理自体は既存方針どおりfail-openを維持する。

### ログ保持と容量上限

- Mac管理CLIの読取り上限256 KiBに対し、サーバー側の運用上限は安全余裕を含む240 KiBとする。
- 1イベントは最大120 KiBとし、直前の有効イベント1件と新イベント1件を必ず240 KiB内へ収められるようにする。上限超過イベントはファイルを変更せず拒否し、メール処理はfail-openを維持する。
- `OperationalLogger`は各追記を日常的なpreflightとして扱う。同じprivate directory内の固定sidecar（`<log>.lock`）を通常file、`0600`、実効UID所有、`nlink=1`として安全にopenし、すべての追記とcompactionで`flock(LOCK_EX)`する。lock取得後に現行logのpathとopen descriptorのinode/dev一致、owner、mode、`nlink=1`、親directory identityを再検証する。logとlockのsymlink、hardlink、所有者差異、親directory差替えを拒否する。
- 追記後に240 KiBを超える場合、または現行末尾が改行で終わらない場合、完全かつJSON objectとして読める末尾行だけを新しい順に選び、直前の最新有効行を必ず含めてから新イベントを加える。同じprivate directoryへ推測困難な名前を用いて排他的に`0600` tempを作成し、完全なJSONLを書いてflushし、可能なら`fsync`した後、size、mode、owner、`nlink=1`とdirectory identityを検証する。現行logを再検証してからatomic renameで置換し、置換後も新descriptorとpath identityを再検証する。
- tempのsymlink化、mode/owner/link差替え、short write、flush、rename前の障害では安全にtempをcleanupして旧logを一切変更しない。rename後の障害では完全な新logだけが見える。通常追記も固定sidecar lock下で行い、途中追記で残り得るpartial tailは次回起動・追記時の強制compactionで除去する。失敗は固定例外へ正規化し、配送処理はログ失敗で停止しない。

## 診断データ

既存の`timestamp`、`outcome`、`message_id_hash`、`classification`、`http_status`に、次の許可済み診断項目を追加する。

- `attempt_count`: Webhook送信回数。
- `attempt_http_statuses`: 各試行のHTTP状態。取得不能時は`null`。
- `provider_code`: LINE WORKSのJSON応答に含まれるスカラーの`code`。文字列の場合は制御文字を除去し最大64文字。
- `provider_description`: JSON応答の`description`。制御文字を除去し最大200文字。
- `response_format`: `json`、`invalid_json`、`transport_error`のいずれか。
- `response_content_type`: 応答ヘッダーから取得し、制御文字を除去して最大100文字。
- `response_body_bytes`: 応答本文のバイト数。
- `response_body_sha256`: 応答本文のSHA-256。本文自体は保存しない。
- `payload_bytes`: LINE WORKSへ送信したJSONのバイト数。
- `title_characters`: 通知タイトルの文字数。
- `text_characters`: 通知本文の文字数。
- `recovered_by_retry`: 初回失敗後の再試行で成功した場合のみ`true`。

診断項目はWebhookの最終結果と、同一送信処理内の試行履歴を表す。再試行で成功した場合も初回エラーの状態・コード・説明を保持し、一時障害だったことを判定可能にする。

`provider_code`と`provider_description`は信頼済みのローカル値ではなく、外部LINE WORKS応答本文から抽出した非信頼入力である。`WebhookClient`と`OperationalLogger`の両境界で型、制御文字、長さ、応答形式との相関を検証する。provider固有の接頭辞・接尾辞を含む場合も、送信payloadのtitleまたはtextとの間に8 bytes以上の共通連続断片がある文字列codeまたは説明は例外なく保存せず`null`とする。整数codeは文字列echo判定を行わず保持する。`sendObserved`から初回、retry、分割chunkのprivate request chainへ実際のtitle/textを渡し、echo判定のためにpayload JSON全体を`json_decode`して複製しない。判定は8-byte window集合を構築して各入力を一度ずつ走査し、payload長とprovider値長の積に比例する反復検索を避ける。残るprovider値も外部入力として扱い、Mac consumerはC0・DEL・C1をfail-closedで拒否し、表示時にもURL、メールアドレス、hash、制御文字を再検査する。これらの多層防御によってメール内容を保存しない契約を維持する。

## 保存禁止情報

次の情報はログへ保存しない。

- メール本文、通知本文、件名、差出人、To、Cc、Bcc
- 添付ファイル名または内容
- Webhook URL
- Xserver APIキー、FTPS・SSH・メールの認証情報
- LINE WORKS応答本文の原文
- 例外メッセージ、HTTPリクエストヘッダー

`provider_code`と`provider_description`以外の応答本文は、サイズとSHA-256だけを記録する。説明に制御文字がある場合は除去し、上限を超える部分は破棄する。

## データフロー

1. `WebhookClient`が各送信試行のHTTP状態と、安全化した応答メタデータを収集する。
2. 1 payloadの送信は明示状態機械で最大2回とする。初回429は0〜15秒の正規`RateLimit-Reset`がある場合だけ1回、初回5xxは5秒後に1回だけ同一payloadを再送する。2回目の429または5xxから追加遷移しない。
3. `DeliveryApplication`が最終成否と診断結果を`OperationalLogger`へ渡す。
4. `OperationalLogger`が項目・型・長さを再検証し、許可済みフィールドだけをJSON Lines形式で追記する。
5. Mac管理CLIの「同期診断」が最新の失敗、または再試行復旧イベントを読み、日本語の項目名で表示する。

## Mac管理CLI表示

「同期診断」に次を追加する。

- 最新障害日時（日本時間）
- 最終結果
- HTTP状態の推移（例：`500 → 200`）
- LINE WORKS応答コード
- LINE WORKS応答説明
- 応答形式
- 再試行で復旧したか
- メール識別ハッシュの先頭12文字

メール内容とメールアドレスは表示しない。診断記録がない旧ログでは「詳細診断情報なし」と表示し、既存ログとの後方互換性を維持する。

## エラー処理

- JSONでない応答は`invalid_json`として記録し、本文はハッシュ化する。
- 接続・TLS・タイムアウトなどHTTP状態を取得できない場合は`transport_error`とし、例外本文は記録しない。
- 診断情報の形式が不正な場合、`OperationalLogger`はその項目を拒否し、未知のフィールドを保存しない。
- `OperationalLogger`はHTTP範囲、outcome/classification/status、非負寸法、試行数、response format、hash、provider metadata、retry復旧相関をMac consumer相当の契約で再検証し、不正イベントではファイルへ1 byteも追記しない。
- 診断ログ書き込み失敗が受信メール処理を停止させない既存のfail-open方針を維持する。

## テスト方針

- HTTP 500から200へ復旧した場合、両試行と`recovered_by_retry=true`が記録される。
- HTTP 500が継続した場合、最終失敗と両試行が記録される。
- `429 → 500`と`500 → 429`はいずれも2送信で停止し、不正なresetでは再送しない。
- JSON、非JSON、トランスポート例外の各応答形式を区別する。
- 応答説明の制御文字除去と長さ上限を検証する。
- Webhook URL、メール本文、メールアドレス、例外メッセージがログへ混入しないことを検証する。
- 既存形式のログをMac管理CLIが読み取れることを検証する。
- `0600`以外のログ、`0700`以外の親、シンボリックリンク、所有者不一致を拒否する。
- 240 KiB境界と改行なしpartial tailで完全JSONL行だけを保持し、最新の既存イベントと新イベントを失わず、成功後も256 KiB未満であることを検証する。固定sidecar lock下の並行writer、tempのsymlink/wrong-mode、short write、flush、rename前後の障害を注入し、置換前は旧logがbyte単位で不変、置換後は完全な新logだけになること、再起動相当の次回追記でpartial tailを回復することを検証する。
- provider説明と文字列codeのASCII・日本語の接頭辞/接尾辞付きecho、無関係な通常値、7-byte以下の共通断片、integer code保持、永続ログ非漏えい、10 MiB payloadの正しさと追加peak memory上限を検証する。
- MLSTはRFC形式の任意reply textを持つ`250-...` / `250 ...` envelopeを許可しつつ、内部のfact entryを厳密に1件、完全path、`type=file`、mode `0600`へ束縛する。factを装う不正行、複数entry、偽envelopeは拒否する。
- 全既存テストと公開リポジトリ向け秘密情報スキャンを実行する。

## 配備と確認

- 夜間には本番配備、Webhookテスト、テストメール送信を行わない。
- 日中に通常のリリース検証を通して配備する。
- 日中配備前preflightで現行ログが256 KiB以下、親`0700`、ファイル`0600`であることを確認し、過大な旧ログは安全なowner確認後に保持方針へ移行する。
- 配備後は秘密情報を含まない管理用テスト通知を1回だけ実施し、Mac管理CLIで試行履歴を読み戻す。
- 実際のHTTP 500が再発した場合、保存されたプロバイダー応答によってLINE WORKS側の入力拒否、一時障害、非JSON応答、通信障害を切り分ける。
