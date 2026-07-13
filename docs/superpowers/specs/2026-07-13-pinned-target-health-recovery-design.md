# 恒久通知対象とLINE WORKS障害復旧通知 設計

## 目的

外部ドメインから基準メールボックスへ転送されるアドレスを、自動検出だけに依存せず恒久通知対象として保持する。また、LINE WORKS Incoming Webhookの障害をprivate領域で記録し、障害発生と復旧を登録済みエラー通知先へそれぞれ一度だけEメールで通知する。

実アドレス、Webhook URL、認証情報は公開リポジトリへ保存しない。本文中のアドレス例には`example.invalid`だけを使用し、本番値はXserverのprivate configにだけ保存する。

## 採用案

### 恒久通知対象

private configへ`notification_pinned_targets`を追加し、Xserver APIから自動検出した転送元との和集合を同期対象にする。コードへのアドレス固定は公開時に自社情報が漏れるため採用しない。ドメイン全体を条件にする方式は意図しないメールを取り込むため採用しない。

`notification_targets`は同期済み全体のreadback集合として維持する。Mac管理CLIの追加・変更・削除は恒久対象だけを変更し、自動検出対象を直接削除しない。

`error_recipients`は最大32件とし、canonical値を`,`で連結したTo値を最大900 bytes、`log_path`を最大4096 bytesに制限する。生成する各header lineは998 bytes未満、署名済みsystem mail全体は65536 bytes以下になることをprivate helperとruntime configの両方で事前検証する。system mailのrequired headerはfoldせず、上限を超えるconfigは保存・起動とも拒否する。

### 障害状態

通常Webhook失敗後、秘密値を含まないエラーWebhookを試す。これも失敗したときだけ障害観測とし、正常から障害中へ遷移する最初の観測でEメールを送る。障害中に通常WebhookまたはエラーWebhookが成功した最初の観測で復旧Eメールを送り、正常へ戻す。

通常稼働と並行実行では各遷移一通にする。sendmail成功直後かつ状態commit前にプロセスが異常終了した場合だけ、通知欠落より再送を優先して重複を許すat-least-once方針とする。

## メールアドレスの正規化

Python CLI、Xserver API client、private-config PHP helper、runtime PHP、scope journalで同じ検証規則を使う。

- local-partはASCII dot-atom、最大64文字で、大文字小文字を保持する。
- domainはASCII DNS label、最大253文字で、小文字化する。
- アドレス全体は最大254文字とする。
- 前後空白、表示名、改行、空label、dot segmentを拒否する。
- 正規化後のbyte順にsortし、完全一致で重複排除する。legacy config移行と対話入力は安全に重複排除し、移行後のpersisted config readbackは既にcanonical・sorted・uniqueでなければ拒否する。
- 自動検出と恒久対象が重複する場合は一つのfilterだけを作る。
- 恒久対象から削除しても自動検出集合に残るアドレスのfilterは削除しない。

## 恒久対象のトランザクション

`ScopeJournal`をschema version 2へ更新し、従来のrule hashと進捗に加えて次をprivate領域のmode `0600`ファイルへAPI変更前に保存する。

- `desired_pinned`
- `desired_targets`
- `config_before_sha256`

処理順序は次のとおりとする。

1. private configと全Xserver filterを読み戻す。
2. 自動検出集合と提案された恒久対象から完全な期待集合を計算する。
3. 期待集合、config digest、無関係ruleを含むjournalを永続化して読み戻す。
4. 新ruleを追加し、API readbackで完全一致を確認する。
5. 古い管理ruleだけを削除し、無関係ruleが不変であることを確認する。
6. private configをCASで更新し、`notification_pinned_targets`と`notification_targets`を読み戻す。
7. APIとconfigの完全一致を確認してjournalを`committed`にする。

中断後はjournalの`desired_pinned`と`desired_targets`を正本として同じ操作を再開する。旧version 1の`committed` journalは読み取り可能とするが、未完了version 1は書き換えず安全停止する。追加は削除より先に行い、未知rule、ID競合、config競合、readback不一致では停止する。

Mac管理CLIは差分を表示し、既存の日本語確認文を要求してからこのトランザクションを実行する。追加・変更・削除は恒久対象だけを操作し、自動検出対象の直接削除要求には理由を表示して受付しない。

## 認証済みシステムメール

固定ヘッダーだけで通知を抑止すると外部送信者が偽装できるため、private configにランダム256-bitの`system_mail_hmac_key`をbase64urlで保存する。既存releaseは未知keyを保持し、新release切替前にMac配備処理が欠損時だけ生成してCAS/readbackする。鍵は画面、ログ、GitHub、メールへ出さない。

`system_mail_hmac_key`は32 random bytesをRFC 4648 base64url、paddingなしの43 ASCII文字で表現する。`[A-Za-z0-9_-]{43}`との完全一致とstrict decode後32 bytesを必須にする。

障害・復旧メールには次の専用ヘッダーを各一つ付ける。値はheader foldingを使わないASCIIとする。

- `X-Xserver-Mail-Notifier-Version: 1`
- `X-Xserver-Mail-Notifier-Type: error`または`recovery`
- `X-Xserver-Mail-Notifier-Event: <lowercase 32 hex>`
- `X-Xserver-Mail-Notifier-Body-SHA256: <lowercase 64 hex>`
- `X-Xserver-Mail-Notifier-Auth: hmac-sha256=<lowercase 64 hex>`

Subjectは`Xserver mail notifier error`または`Xserver mail notifier recovered`に固定する。Dateは英語曜日・月名を使う`D, d M Y H:i:s +0000`形式とする。Toは正規化済みエラー通知先をbyte順にsort・uniqueし、`,`一文字で連結する。wire上のheader/body境界はCRLF二組とし、生成本文は最後にCRLFを一つだけ持つ。署名用本文はCRLFとlone CRをLFへ変換し、末尾LFを削除も追加もせず、UTF-8 bytesのSHA-256を取る。検証側も最初のRFC 5322 header/body境界より後の全bytesへ同じ変換を適用する。

HMACのfield順はversion、type、event、canonical To、Subject、Date、body SHA-256とする。各fieldをUTF-8 bytesとして、ASCII十進byte長、`:`、field bytes、LFの順に連結し、decode済み32-byte keyでHMAC-SHA-256を計算してlowercase hexにする。

runtimeは入力直後、最大65536 bytesのheader prefix内で最初のheader/body境界を探し、MIME解析、dedup予約、health sequence予約、lock、Webhookより前に、必須ヘッダーが各一件だけであること、形式、TypeとSubjectの対応、Date、To、本文hash、HMACを確認する。境界が上限内にない場合はsystem mailとして扱わない。すべて一致した場合だけシステムメールとしてWebhookを抑止し、allowlist分類だけをログへ残す。

固定ヘッダー単独、欠損、重複、改変、別本文との組み合わせ、無効HMACは通常メールとして処理する。これにより外部送信者による通知回避と、システムメールが通知対象へ届いた場合の再帰を同時に防ぐ。

## 状態ファイルと並行実行

状態はoperational logと同じprivate directoryの`delivery-health.json`へ保存し、専用lock fileを使う。両ファイルはownerが実効ユーザー、regular file、no symlink、mode `0600`でなければならない。親directory chainにも既存private trust規則を適用する。

状態ファイルは最大4096 bytes、UTF-8 strict JSON object、duplicate keyと未知keyを拒否する。schemaは次を持つ。

- `schema_version`: `1`
- `status`: `healthy`または`degraded`
- `changed_at`: UTC日時
- `next_observation_sequence`: 0以上`PHP_INT_MAX`以下
- `last_applied_sequence`: 0以上で`next_observation_sequence`以下
- `degraded`時だけ`classification`と64桁hexの`message_id_hash`

欠損時はexclusive lock内で現在UTCを`changed_at`に持ち、`next_observation_sequence=0`、`last_applied_sequence=0`の`healthy`をatomic createし、mode、hash、bytesを読み戻す。不正schema、oversize、unsafe path、権限不正、I/O失敗では`health_state_failure`だけをallowlist logへ記録し、通常Webhook配送は止めない。ただし状態を安全に判定できるまで障害・復旧Eメールは送らず、通知洪水を防ぐ。Mac診断はこの異常を明示する。

Mac診断と本番readbackは、固定private-config helperの`health-summary`操作だけを使う。この操作はconfigから導出した固定health path以外を受け付けず、missing、healthy、degradedの状態、`changed_at`、classification、二つのsequenceだけを厳密schemaで返す。Message-ID hash、アドレス、path、鍵、Webhook URLは返さない。不正・unsafe stateは固定エラーでnonzero終了し、Mac側は「状態ファイル不正」とだけ表示する。

各通常WebhookとエラーWebhookは、ネットワーク開始直前に短いexclusive lockで単調増加sequenceを予約・atomic commitしてからlockを解放する。`next_observation_sequence`は予約済み最大値を表す。予約時は現在値が`PHP_INT_MAX`なら失敗し、それ以外は一つ加算してcommitした値を返すため、最初のsequenceは`1`、最後に予約可能なsequenceは`PHP_INT_MAX`となる。sequence予約は`next_observation_sequence`だけを進め、`changed_at`と`last_applied_sequence`は変更しない。上限到達後は`health_state_failure`をログへ記録し、そのHTTP送信自体は行うが追跡不能観測として障害・復旧遷移には使用しない。

HTTP結果確定後、`sequence <= last_applied_sequence`なら古い観測として破棄する。`sequence > last_applied_sequence`なら、status遷移がない`healthy`中の成功と`degraded`中の失敗を含め、必ず`last_applied_sequence`をそのsequenceへatomic commitする。`changed_at`は障害または復旧の遷移がsendmail成功後にcommitされたときだけ更新する。新しい観測だけが状態遷移を行うため、後発の成功が先に確定した後で古い失敗が状態を障害中へ巻き戻すことはない。

通常Webhook失敗だけでは状態を変更せず、その後のエラーWebhookのsequenceと結果を最終的な疎通観測にする。

遷移時はexclusive lockを保持してsendmailを最大15秒で実行し、成功後にstatus、`changed_at`、`last_applied_sequence`をatomic replace/readbackする。sendmailへ渡すmessageは最大65536 bytesとする。stdoutとstderrは停止判定のため最後までdrainするが、それぞれ保持するdiagnostic bytesは8192 bytes以下とし、内容をログや例外へ出さない。sendmail失敗時はstatusと`changed_at`を維持したまま`last_applied_sequence`だけをcommitし、次の新しい有効な観測で再試行する。lockをsendmail完了まで保持して並行重複を防ぐ。

期限付き強制障害テストは外部ネットワークを開始しない。対象件名がtokenと完全一致した場合、通常WebhookとエラーWebhookを呼ばず、エラーWebhook相当のsynthetic observation sequenceを一つ予約して`forced_test_failure`として`recordFailure()`へ渡す。これにより障害Eメール、連続抑制、次の実Webhook成功による復旧Eメールを同じ状態機械で検証する。

classificationは次の完全なallowlistだけを状態、Eメール、ログへ渡す。その他の値、例外class、空文字、64文字超過は`unknown`へ写像する。

- `success`
- `invalid_payload`
- `invalid_parameter`
- `missing_parameter`
- `invalid_webhook_url`
- `rate_limited`
- `http_error`
- `transport_error`
- `forced_test_failure`
- `internal_error`
- `system_mail_suppressed`
- `health_state_failure`
- `unknown`

## Eメール内容

障害・復旧Eメールは登録済み`error_recipients`全員へXserverの`/usr/sbin/sendmail -t -i`で送る。外部SMTP設定は使わない。

本文に含める情報は次だけとする。

- 障害発生または復旧のUTC日時
- 分類済みエラーコード
- Message-IDの一方向hash
- private operational log path

元メールの件名、本文、From、To、Cc、Bcc、Webhook URL、API key、FTPS/SSH認証情報、例外文は含めない。自動再送queueは作らず、メール原本はコピー転送先mailboxを正本とする。reporter初期化前のframe/config障害は秘密設定を安全に利用できないため、従来どおりnonzero終了とする。

## 配備順序

1. Mac管理アプリを更新する。
2. 現行helperを使い、legacy configのアドレス配列をcanonical・sorted・uniqueへ移行し、空または既存の`notification_pinned_targets`と、欠損時だけ生成した`system_mail_hmac_key`をCAS/readbackする。この手順では本番の恒久通知対象を追加しない。
3. fixed runtimeを検証済みprefixとgeneration別backupだけで新private-config helperへ移行する。
4. 新releaseをstageし、strict validatorとremote validationを通す。
5. active locatorを切り替える。
6. version 2 journal transactionで恒久通知対象、Xserver filter、`notification_pinned_targets`、`notification_targets`を一体として同期する。
7. API、private config、release manifest、固定runtime、mode、public_html不使用を読み戻す。
8. 強制障害テストで障害Eメール一通、連続失敗時の抑制、Webhook成功後の復旧Eメール一通、連続成功時の抑制を確認する。

秘密値、実アドレス、メール本文は検証出力へ出さない。

## テスト

実装はTDDで行い、少なくとも次をred-greenで検証する。

- 恒久対象と自動検出対象の和集合、追加・変更・削除、local-part保持、domain小文字化
- scope journalの全中断点からの再開、旧schema処理、無関係rule不変、CAS競合
- HMAC正常系、固定ヘッダー偽装、重複ヘッダー、本文改変、header replayと別本文
- システムメール判定がMIME、dedup、health、Webhookより前に終了すること
- 状態のstrict schema、mode、symlink、owner、oversize、I/O failure
- sequence予約と古い結果の破棄、並行障害・復旧の重複抑制
- sendmail失敗時の再試行、障害中の抑制、復旧後の一度だけ通知
- エラー・復旧メールとログに秘密値や元メール情報が含まれないこと
- 全PHP/Pythonテスト、PHP syntax、public secret scan、Mac bundle検証

実装タスクごとの独立レビューと、配備前のwhole-branch reviewでCritical・Important・Minorが0件であることを本番反映の条件とする。

## 承認済みaddendum: 旧version 1 target-sync journalの限定closure

メニュー13の自動同期（`sync_targets()`へ恒久対象案を渡さない呼び出し）だけは、API削除が成功した直後のreadback中断で残った旧version 1 journalを、次の全条件が同時に成立するときだけjournal-onlyで閉じてよい。メニュー2・3・4は従来どおり未完了version 1を拒否する。

- journalはschema version 1、`active`、単一の`delete` pendingであり、そのIDとbody hashは`old`の同一要素と完全一致する。
- `old`の全IDは現行APIから消失している。`new_ids`は全IDが現行APIに存在してbody hashが完全一致し、そのhash集合が`new`と完全一致する。
- journalに記録された無関係IDが現行APIに残る場合はfull-rule hashが完全一致する。既知IDの内容変更・再利用、API ID重複は拒否する。
- journal外の現行filter ID集合は、同じ自動検出集合から作るclean planのretire ID集合と完全一致し、clean planのaddは0件でなければならない。任意の未知filterは拒否する。許可されたretire予定filterと、履歴上の無関係filterの欠損はそれぞれ件数だけを警告し、ID・address・bodyを表示せず、このclosure中のAPI変更は認可しない。
- private configのcanonicalな`notification_targets`が自動検出集合と完全一致し、その集合から生成する管理rule body hash集合がjournalの`new`と完全一致する。恒久対象その他の未知config fieldは変更しない。

専用確認文は`旧同期journalを完了として閉じる`とする。確認前後でjournalの正確なbytes、private configの全値とSHA-256、APIのcanonical full snapshotを二重readbackして完全一致を要求する。確認後の唯一のmutationは現在の`PrivateConfigSsh`経由のtarget-sync journal CAS一回であり、`phase=committed`、`pending=null`、`retired_ids=oldの全ID`とする。API add/deleteとconfig CASは0回で、同じ呼び出し中にversion 2 transactionを開始しない。

fixed private helperへ`scope-journal-compare-and-swap`を追加する。requestはexpectedをmissingまたはpresent exact body+SHA-256で表し、desired exact body+SHA-256を渡す。最大二つの64KiB journalをbase64で同時に運ぶためrequest上限を262144 bytesへ有界拡張する。helperは既存のglobal config lockとjournal transaction lockの内側でlogical recoveryを完了し、current bytesをexpectedとexact比較してからだけwriteする。応答statusは`changed`、`already_applied`、`conflict`だけとし、body/hashは出力しない。旧`scope-journal-write`はtarget-syncを拒否し、filterだけに残す。`ScopeJournal`の全target-sync writeは、直前readで得たexplicit raw snapshot tokenをexpectedにしたCASを必須とする。

transport failure、malformed response、`conflict`では必ずjournalを再readする。desired exact bytesなら成功、expected exact bytesなら未適用としてretry可能な明示エラー、第三の値ならambiguousとして明示停止する。write後にAPIまたはconfigのdriftを検出した場合、closure自体はcommit済みでありrollbackせず、追加mutationを一切行わず「完了後の外部変更」として停止する。closure後の別呼び出しは、committed version 1を監査記録として扱い、現行snapshotから新しいversion 2 transactionを開始する。
