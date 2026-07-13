# Xserverメール通知管理 Macアプリ設計

## 目的

既存の日本語対話式Mac管理CLIを、Finderからダブルクリックして起動できるMacアプリとしてインストールする。Xserver APIキー、FTPSパスワード、Webhook URLなどの秘密情報をアプリやGitHubへ含めず、既存のキーチェーン運用を維持する。

## 利用者体験

- `~/Applications/Xserverメール通知管理.app` をダブルクリックする。
- ターミナルが開き、既存の日本語管理メニューが表示される。
- メニュー終了後は終了結果を日本語で表示し、利用者の確認後にウィンドウを閉じられる。
- 設定不足、Python不足、キーチェーン読取失敗は、秘密値を表示せず日本語で案内する。

## 構成

### アプリ本体

標準のmacOSアプリバンドルを生成するインストーラーをリポジトリへ追加する。アプリはAppleScriptからターミナルを起動し、アプリ内へ同梱した固定パスのシェルランチャーを実行する。外部の有償ツールや追加アプリには依存しない。

アプリ内には次だけを同梱する。

- `manager/manage.py`
- `manager/keychain.py`
- `manager/ftps_deployer.py`
- `manager/xserver_api.py`
- 起動ランチャー
- 公開可能な説明・バージョン情報

この一覧は業務ロジックの同梱物を示す。標準アプリバンドルとして必要な`Info.plist`、実行ファイル、リソース、アイコンは含める。bundle IDは`jp.example.xserver-mail-lineworks-manager`へ固定し、ローカル入力を含む上書きを許可しない。

### Python実行環境

対応Pythonは3.13以上3.14以下とする。現行CLIが使用する`email.utils.parseaddr(..., strict=True)`を確実に利用できる版だけを許可する。

インストーラーは自身を実行しているPythonの実体を`realpath`で解決し、版を検証する。アプリのローカル生成時に検証済みPythonの絶対パスをランチャー専用メタデータへ固定し、起動時に同一実体・対応版であることを再検査する。`PATH`検索、`env python3`、ユーザー設定値によるPythonパス指定は行わない。対応Pythonがない場合はアプリを作成せず、Python公式インストーラーまたはHomebrewでPython 3.13か3.14を導入する日本語案内を表示する。Pythonランタイム自体はアプリへ同梱しない。

### ローカル設定

実環境固有だが秘密ではない設定は、ユーザー専用の次のファイルへ保存する。

`~/Library/Application Support/XserverMailLineworks/config.json`

設定項目は以下とする。

- Xserver初期ドメイン
- PHPスクリプトのサーバー絶対パス
- FTPSホスト
- FTPS基準の秘密設定パス
- Xserver上のホームディレクトリ
- MacのSSH設定に登録済みの接続alias

JSONキーと環境変数の対応は次で固定する。

- `servername` → `XSERVER_SERVERNAME`
- `command_path` → `XSERVER_COMMAND_PATH`
- `ftps_host` → `XSERVER_FTPS_HOST`
- `config_path` → `XSERVER_CONFIG_PATH`
- `filesystem_home` → `XSERVER_HOME`
- `ssh_alias` → remote validatorの`/usr/bin/ssh`接続先（環境変数へexportしない）

必須キーは上記6個だけとし、未知キーは誤設定として拒否する。全値は文字列かつ非空で、改行、NUL、制御文字を拒否する。`servername`はXserver初期ドメイン形式、`ftps_host`はDNSホスト名だけを許可し、ユーザー情報、ポート、URLを拒否する。`ssh_alias`はMacのSSH設定に利用者が登録した非秘密aliasだけを許可し、空白、先頭hyphen、`@`、slash、shell metacharacterを拒否する。接続先、ユーザー、ポート、鍵pathは設定JSON、アプリ、Git、logへ複製しない。3つのパスは絶対パス、dot segmentなし、`public_html`なしとする。

設定ファイルはディレクトリ`700`、ファイル`600`とする。アプリのインストール時に対話式で作成し、既存設定がある場合は明示確認なしに上書きしない。

起動時はApplication Supportから設定ファイルまでの各親要素を`lstat`し、シンボリックリンクでないこと、所有UIDが現在ユーザーであること、期待モードと一致することを検査する。ファイルはシンボリックリンク非追従で開き、open後の`fstat`結果が事前検査と同じinode・device・所有者・モードであることを確認してから、そのファイル記述子から上限64 KiBで読み取る。検査とopenの間に置換された場合は拒否する。

### 秘密情報

APIキーとFTPS認証情報は既存のmacOSキーチェーン項目から実行時に読み取る。Webhook URLとエラー通知先はXserverの非公開設定から取得する。秘密値をアプリバンドル、ローカル設定、コマンド引数、標準出力、ログ、Git履歴へ保存しない。

Keychain許可・拒否testはrepository外の専用一時keychainと限定ACL itemだけを使う。global keychain search list、default keychain、login keychain ACLは変更しない。`Keychain`はproductionではkeychain pathを渡さず、test dependency injection時だけ検証済み一時keychain絶対pathを`security` argvへ明示する。環境変数や利用者設定からこのpathを指定できない。

### Xserverの安定エントリーポイントとversioned release

メール振り分けのcommand targetは、releaseディレクトリ内のPHPを直接指さず、非公開領域の固定pathへ一度だけ配置するstandalone bootstrapを指す。bootstrapはComposer、autoload、release内classへ依存せず、locatorの完全schema検証を単一実装として自身に含む。bootstrapから固定されるlocator pathだけを開き、locator内で初回固定した既存秘密config pathとversioned entrypointを使う。releaseへconfig、Webhook URL、メールアドレスその他の秘密を複製しない。

locator、bootstrap、release、config、logはすべて`public_html`外に置く。bootstrapは初回移行後immutableとし、通常release更新やrollbackで上書きしない。locatorは全release世代で後方互換な`schema_version/release_id/release_path/entrypoint/manifest_sha256/config_path`だけの完全schemaを維持し、未知field/versionを拒否する。`config_path`は初回に既存pathへ固定し、通常更新ではold locatorとbyte一致しなければ拒否する。通常更新のremote契約は、同一directoryの完全なtempを単一renameしてoldまたはnewだけを可視化し、downloadしたbytes/hash/schema/modeをreadbackすることに限定する。FTPSにはremote file/directory `fsync`がないためserver crash後の永続性はXserver基盤依存で保証しない。次回はlocatorをreadbackし、valid old/newならrollback/forward、partial/未知bytesならfilterを変えず停止する。Macローカルinstallerのfile/directory `fsync`契約とは別である。

remote absolute layoutは次へ固定し、他の場所へfallbackしない。既存秘密configは移動せず従来pathを使い、locator/releaseへ複製しない。

| 用途 | `filesystem_home`基準の固定path |
|---|---|
| bootstrap | `<home>/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php` |
| locator | `<home>/private/xserver-mail-lineworks/state/active-release.json` |
| 既存秘密config | 既存`config_path`の絶対path（非移動） |
| release | `<home>/private/xserver-mail-lineworks/releases/release-<SHA>` |
| deploy transaction | `<home>/private/xserver-mail-lineworks/deploy-transactions/<txn-id>` |

旧layoutからの初回移行は、全recipient/domain/ruleのmanaged filter集合を一つのreadback/recovery transactionとして扱う。Xserver公式仕様で同一メールに複数ruleがmatchした場合の評価順序とaction実行数を確認し、旧/new同時存在でも一回だけ実行されることをfixtureと実環境probeで確認できる場合だけoverlap方式を使う。保証できなければ短時間maintenanceを宣言し、旧集合を削除・0件readbackしてからnew集合を追加するためold/newを同時存在させない。maintenance中はLINE WORKS通知が停止し得ることを従業員へ事前表示し、copy転送の原本はmailboxへ残す。mailbox取得権限を追加せず、自動再送や通知欠落0を偽って保証しない。maintenance中の二重通知0件、原本消失0件をE2E確認する。以後のversion切替えはfilter不変でlocatorだけを更新する。

FTPSはuploadとdownload hash readbackを担当する。認証済みSSH alias経由のremote PHP CLIは、private manifestのrelative path/type/size/SHA-256/exact mode/symlink、PHP version/extensions、autoload、absolute CLI dry-runを検証する。server home監査modeは`public_html`の非追従metadataと既知製品basename一致件数だけを返し、未知fileの内容・hash・名前を読まない。SSHは`/usr/bin/ssh`の固定argv、BatchMode、明示alias、固定validator commandだけを使い、秘密値や接続詳細をargv/stdout/logへ出さない。

SSH trust boundaryは現在UID所有の`~/.ssh` `0700`、非symlinkのmain `config` `0600`、owner-only固定`known_hosts` `0600`とする。`ssh -G`より前にmain configをbounded raw parserで読み、directive位置の`Include`、全`Match`、全`HostKeyAlias`をASCII大小文字、global/Host内、indent、quoted/comment/escaped boundary、継続行を問わず拒否する。parserはunquoted/unescaped comment境界を認識し、odd backslashの論理行を結合し、quote/escape不整合・非UTF-8・NUL・oversizeをfail closedにする。aliasはwildcard/negation/複数patternではなくexact単独`Host` stanzaで定義されることを要求する。

実行と同じ固定overrideで`/usr/bin/ssh -G -- <alias>`を実行し、effective `hostname`が`ftps_host`または`servername`と一致すること、`hostkeyalias`が未設定/default、`proxycommand/proxyjump/knownhostscommand/remotecommand/localcommand`がnone、canonicalization/forward/control masterが無効、user known-hosts pathが固定owner-only fileであることを内部検査する。actual SSH argvへ`HostKeyAlias`を渡さず、known_hostsはresolved Xserver hostnameで照合する。`ssh -G`全文、config/known_hosts path/content、user、identity path、expanded hostname、stderrは表示・保存しない。unknown/changed host key、symlink、緩いmode、wrong owner、wildcard alias、hostname mismatch、parse failureでは固定分類で停止する。

Includeを全面拒否するため検査対象はmain configと固定known_hostsだけである。両fileはraw parse/`ssh -G`後とactual SSH spawn直前の2回、`lstat` identity/mode/owner/size/hashを再照合する。ただしOpenSSHへ検査済みfdを渡せずpathを再openするため、その最終再照合後の差替えを完全には封じられない。この限界をREADMEへ明記し、identity差替えを検出した場合は実行しない。

一時validator mail filter、合成mail、nonce receiptはSSHで不要なため作成しない。Xserver APIは初回の本番managed filter集合移行とreadbackだけに使い、通常release validation/switchではfilter API mutationを0回とする。FTPSにはremote `fsync`がないためserver crash durabilityは保証せず、SSH validation成功後もsame-directory rename/readback/old-new recoveryの制約を維持する。

## 起動フロー

1. アプリが固定ランチャーを起動する。AppleScriptがTerminalへ渡す文字列は、アプリバンドル内ランチャーの固定絶対パスをOS標準の安全な引用方法で囲んだものだけとする。設定値や環境変数をAppleScriptへ連結しない。
2. ランチャーがユーザー専用設定ファイルの存在、親ディレクトリ、所有権、権限、JSON形式、キーと値を安全に検査する。
3. インストール時に固定したPython実体と版を再検査する。
4. 必須設定を固定対応表に従って環境変数へ設定する。値をシェルコードとして評価せず、子プロセス環境へ直接渡す。
5. 同梱管理CLIを起動する。
6. 管理CLIがキーチェーンとXserver非公開設定を検査する。
7. 日本語メニューを表示する。
8. Terminal内ランチャーが終了コードを受け取り、成功・失敗を日本語で表示してEnter入力を待つ。AppleScriptアプリは非同期起動だけを担当し、終了コードを受け取らない。

設定ファイルがない場合は、秘密情報を尋ねず、インストーラーを再実行する案内を表示する。

管理CLIの`main()`は、キーチェーン読取、APIクライアント生成、FTPSクライアント生成、リモート設定読取、メニュー実行をトップレベルの秘密非露出例外境界で囲む。既知エラーと予期しないエラーはいずれも固定の日本語分類だけを表示し、トレースバック、例外本文、秘密値を表示しない。

## インストールと更新

- リポジトリ内の日本語インストールスクリプトを一般ユーザーとしてMacで実行する。スクリプト全体を`sudo`で起動した場合は拒否する。
- 設定作成、Python検査、一時領域でのアプリ構築、AppleScriptコンパイル、秘密走査は一般ユーザー権限で行う。
- 配置先は現在ユーザー所有の`~/Applications/Xserverメール通知管理.app`へ固定する。installer、更新、回復、uninstallerはすべて一般ユーザー権限で動作し、Authorization Services、`sudo`、管理者承認、privileged helperを使用しない。
- 再インストール時はアプリ本体だけを更新し、ユーザー専用設定とキーチェーン項目を保持する。
- 更新時は既存アプリのbundle ID、現在UID所有、実体パスを検査する。`~/Applications`と同一ファイルシステムのユーザー専用transaction領域へ検証済み新バンドルを用意し、`renameatx_np(RENAME_SWAP)`で旧版とatomic交換する。置換失敗時は旧版を残し、部分更新を公開しない。
- config変更前にconfig/app共通のdurable transaction markerを作る。config二段rename、app交換、commit、cleanupの各途中で終了しても、記録済みtxn ID、generation、dev/inode、hashと実filesystemを照合し、旧app/旧configまたは新app/新configの組へ収束させる。正常なwrite-ahead窓や段階cleanupは隔離せず再開し、identity不一致だけをfail closedにする。
- config回復は既存設定のrename前・rename間・rename後と、初回設定のpublish前・publish後を、それぞれapp absent・old・newと組み合わせた完全判定表に従う。markerとidentityが一致する組だけをold側またはnew側へ収束し、表外の組はbytes/inodeを変更せず隔離する。初回設定tempだけが残りappがnewならconfigをforward公開する。
- recovery自身の各rename/unlink前にもdurable recovery phaseへ操作前後のexact filesystem snapshotを記録する。次processはpre状態なら操作を再実行、post状態なら次phaseへ進み、configをtrashへ移した直後を含む回復途中crashを隔離せず再開する。
- アプリはローカル利用向けの未署名バンドルとする。対応対象は現在利用中のmacOSとし、`osacompile`、`plutil`、実行権限、bundle ID、主要同梱物を検証する。初回起動でGatekeeper確認が出た場合の正規手順をREADMEへ記載し、隔離属性の無効化コマンドは案内しない。
- アンインストールはアプリ本体だけの削除を既定とする。ユーザー設定削除は明示的な別操作、キーチェーン項目は既定で保持する。削除前に固定パス、所有者、bundle IDを検査し、シンボリックリンクを辿らない。

## セキュリティ

- `public_html`を含むサーバーパスを大小文字を区別せず拒否する。
- ローカル設定のシンボリックリンク、グループ・他ユーザー権限、不正JSONを拒否する。
- アプリ内スクリプトは固定引数で起動し、設定値をシェルコードとして評価しない。
- アプリバンドルへ秘密値が含まれないことをインストール後に走査する。
- 既存の公開秘密情報スキャンへアプリ作成物と設定サンプルの検査を追加する。
- キーチェーンACLにより初回アクセス確認が表示される場合がある。許可対象と拒否時の再試行方法を日本語で案内し、許可を自動化しない。
- 秘密走査は既知Webhook形式、APIキー形式、実環境識別子、実メール、秘密設定ファイル名、コマンド引数、ログ出力を対象とする。未知形式の秘密を完全検出できない限界をREADMEに明記する。

## テスト

- 設定ファイルの生成、権限、既存設定保護をテストする。
- 不正パス、シンボリックリンク、緩い権限、不正JSONを拒否するテストを追加する。
- アプリバンドル内に必要ファイルだけが存在することを検査する。
- ランチャーが実設定値をコマンド文字列へ埋め込まないことを検査する。
- 既存のPHP・Pythonテストと秘密情報スキャンをすべて維持する。
- 実Macでダブルクリックし、日本語メニュー表示と安全な終了を確認する。
- Python 3.12以下の拒否、3.13と3.14の受理、固定Python実体の置換検出をテストする。
- AppleScriptコンパイル、固定ランチャーパスだけのTerminal起動、非同期起動、Terminal内での終了コード表示とEnter待ちを検査する。
- 空白を含むユーザーHOME、悪意あるJSON値、64 KiB超過、設定ファイルと親ディレクトリのsymlink、所有者・権限不一致、検査中の置換をテストする。
- installer/uninstallerが昇格を一切要求しないこと、既存アプリのbundle ID・owner不一致、更新失敗時の旧版保持、transaction state破損時のfail-closed回復をテストする。
- state確定後pointer確定前、configの各rename前後、初回設定の有無、app交換前後、CLEANED後の各削除、uninstall trash削除の全fault pointを別processで回復し、通常crashが隔離されずmixed app/configを残さないことをテストする。
- 既存/初回config 5状態とapp absent/old/newの全15組を別processで回復し、定義済み組は非mixedへ収束、identity不一致組は全bytes/inode不変で隔離されることをテストする。
- recovery action全7種のrename/unlink直後にprocessを終了し、次の別processがdurable phaseのpost snapshotを認識して非隔離・非mixedへ収束することをテストする。
- キーチェーン、API、FTPS、予期しない例外でトレースバックや秘密値が表示されないことをテストする。

## 対象外

- 完全なネイティブGUIへの作り直し
- iPhone対応
- 自動アップデート
- APIキーやFTPSパスワードの再登録
- 問い合わせ対応状況管理画面
