# Xserver PHP 標準入力フレーム修正設計

## 背景と根因

Xserverのメール振り分けからstable bootstrapを起動すると、メールはメールボックスへコピー保存される一方、LINE WORKS通知と運用ログが発生しない。実環境の最小再現により、XserverのPHP 8.5 CLIは起動時に標準入出力以外の追加ファイル記述子を閉じることを確認した。既存bootstrapは検証済み秘密設定をFD 3で子PHPへ渡すため、子の`NotifierConfig::load('/dev/fd/3')`が`Configuration unavailable`となる。reporter初期化前の例外は通常配送を止めないためexit 0となり、通知もログも残らない。

## 採用方式

stable bootstrapから子PHPへの入力を、1本の標準入力上に構成する。

1. 固定マジック
2. 8-byte big-endian設定長（上位32bitは0、下位32bitは2〜65536）
3. 検証済み設定JSON
4. メール原文（最大10 MiB + 超過検出1 byte）

bootstrapは従来どおり設定ファイルの所有者・権限・同一性を検証してから内容を読む。子へは非秘密の固定フラグ`MAIL_NOTIFIER_STDIN_FRAME=1`だけを渡し、Webhook URLをargv・環境変数・ファイルパスへ出さない。フレームはowner-onlyのunlinked一時ストリームへ上限付きで構築してrewindし、子のFD 0として渡す。これによりpipeの相互待ち、部分書込み、子の早期終了によるEPIPEを避ける。

子はフラグの完全一致時だけフレームを解析する。マジック不一致、長さ不正、途中EOF、JSONのscalar/list、既存設定schema不正はfail closedとし、legacy入力へフォールバックしない。設定は`NotifierConfig::fromArray()`で再検証し、残りのbytesだけをメール原文として扱う。CRLF、NUL、添付相当binaryを変換しない。フラグなしの既存CLIは、従来の安全な設定パス方式を維持する。

## 影響範囲

- `bin/stable-mail-entrypoint.php`: active release manifestの全runtime recordを検証した後、`src/StdinFrame.php` recordがあるreleaseだけ検証済み設定と上限付きメールをstdin frameへ格納する。recordのない検証済みlegacy releaseは移行期間中のみconfig FDを使い、frame失敗時にlegacyへ降格しない。
- `bin/mail-to-lineworks.php`: frame decoderを使って設定とメールを分離する。
- `src/NotifierConfig.php`: JSON bytesから既存schema検証へ接続する小さな入口を追加する。
- `bin/validate-release.php`と`src/ReleaseValidator.php`: 本番と同じframe方式でdry-runする。
- `--check-config`と`--check-message`だけをallowlistしてstable childへ転送し、未知引数は拒否する。
- 既存のfixed runtimeから更新する際は、[fixed runtime migration design](2026-07-13-fixed-runtime-migration-design.md)の世代固定・backup・prefix検証に従う。dual bootstrapは検証済みactive release manifestの`src/StdinFrame.php` recordで旧方式とframe方式を選択する。

## エラー処理と安全性

- 設定は最大64 KiB、メールは最大10 MiB + 1 byte。無制限にメモリや一時ストリームへ読み込まない。
- 一時ストリームはunlinkedで、公開領域へファイルを作らない。
- frame flag時は旧`MAIL_NOTIFIER_CONFIG_FD`と`MAIL_NOTIFIER_CONFIG`を無視する。
- protocol判定はfixed treeの存在やhashではなく、完全検証済みactive release manifestの`src/StdinFrame.php` recordだけを使用する。
- config bytesをメールhash、通知本文、運用ログ、stderrへ混入させない。
- bootstrap検証失敗または子の非0終了は従来どおり固定文言・非0終了とする。通常の通知エラーはメールボックス保存を妨げない。
- frame decodeまたは設定構築がlogger/reporter初期化前に失敗した通常配送は、秘密や例外詳細を出力せず非0終了する。reporter初期化後の通知失敗は、既存どおり設定済み経路で報告を試み、報告失敗がメールボックス保存を妨げない。

## テストと完了条件

テストを先に失敗させ、次を確認する。

- Xserver同様にFD 3を閉じる子PHPでも、frameから設定とメールを読める。
- 1-byte刻み相当のpartial read、途中EOF、magic/長さ/JSON/schema不正を拒否する。
- CRLF、NUL、先頭がmagicと同じ通常メール、binaryをbyte-for-byte維持する。
- 10 MiB境界、legacy CLI、check-config、check-message、未知argvを検証する。
- argv・env・stdout・stderr・運用ログへ秘密設定を出さない。
- 全ローカルテスト合格後に新releaseを配備し、恒久header filterへ一意Message-IDの通常メールを送り、メールボックス保存と運用ログHTTP 200を確認する。
- 一時filter、診断PHP、markerをすべて削除し、恒久3ルールだけをAPIで読み戻す。

過去に欠落したメールは自動再送しない。修正後に新規受信したメールだけを通知対象とする。
