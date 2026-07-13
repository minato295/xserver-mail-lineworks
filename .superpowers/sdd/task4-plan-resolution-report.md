# Mac Task 4 Plan Resolution Report

## 結果

`docs/superpowers/plans/2026-07-11-macos-launcher-app.md`のTask 2–5を、Task 4事前監査の指摘に合わせて修正した。コードは変更していない。

## 計画へ反映した契約

- Task 2に公開`validate_config(value: object) -> dict[str, str]`を追加し、Task 4開始前にprivate validatorを公開・直接テスト・コミットするextensionを追加した。
- Task 3に共通`record_python_runtime()`を追加し、installerと起動時検証が同じ`RuntimeRecord`、canonical path、device/inode、Python 3.13/3.14規則を使うようにした。
- `osacompile`、`plutil -lint`、bundle executable検査をTask 3完了gateへ移した。Task 4はこのgate成功を前提にする。
- Task 4から`install_bundle()`と`/Applications`配置を除き、一般ユーザー権限のbundle build、bundle検証、設定transactionだけに限定した。配置と昇格はTask 5の責務にした。
- bundle Resourcesへ`runtime.py`と同階層の`local_config.py`、`launcher.sh`、`python-path`、`python-runtime.json`、manager 4ファイルを明記した。
- bootstrapのPython候補を3.14→3.13、python.org framework→Homebrew Apple Silicon→Homebrew Intelの6本へ固定し、PATH検索と環境変数追加を禁止した。
- 設定保存をHOMEからの`O_DIRECTORY | O_NOFOLLOW` dirfd chain、app dirfd相対temp/backup/rename、file `fsync`、directory `fsync`、commit/rollbackを持つ`ConfigTransaction`として具体化した。
- source root、入力file、build rootのowner、mode、symlink、device/inode、canonical path trust検査を追加した。
- `osacompile`生成物を含むbundle file exact allowlistを列挙し、未知file/type/symlinkを拒否する契約を追加した。
- 生成済みbundleのtextとbinary (`main.scpt`, `applet.rsrc`, `applet`) を秘密scanner対象にし、scannerの一致内容を例外へ含めないよう指定した。
- bundle IDを`jp.example.xserver-mail-lineworks-manager`へ固定し、sourceは現在ユーザー、配置後はroot、既存appはrootまたは呼出ユーザーownerだけを許可する検査を明記した。
- Task 5へ`install_bundle()`と`install_with_config()`を追加し、build → bundle検証 → 設定durable配置 → app atomic配置 → 設定commitの順序、および配置失敗時の設定rollback・旧app保持を明記した。
- 同一`/Applications` filesystem内のtemp/backup rename、配置後再検証、失敗時旧版復元を具体化した。

## 自己監査

- 必須項目coverage: bundle package layout、AppleScript compile gate、Task 4/5境界、公開config API、dirfd atomic config、runtime record共有、bootstrap固定候補、exact allowlist、生成物秘密走査、source/build trust、設定/配置順とrollback、owner/bundle IDをTask 2–5へ割り当てた。
- interface整合: Task 4はTask 2の`validate_config()`とTask 3の`record_python_runtime()`をconsumeし、Task 5はTask 4の`validate_bundle()`と`ConfigTransaction`をconsumeする。
- placeholder scan: `TBD`、`TODO`、`implement later`、`fill in details`、未確定interfaceを残していない。

## 検証

- `git diff --check`: 成功（出力なし）。
- plan対象の差分を目視確認し、変更はplanと本報告だけであることを確認した。

## 懸念

Task 3の実compiler gateを監査中に実行したところ、現行`macos/AppLauncher.applescript`は次のエラーで失敗した。

```text
macos/AppLauncher.applescript:2: error: Expected “,” but found “\"”. (-2741)
```

したがってTask 3は静的テストとコミット済みでも、修正後plan上は未完了である。Task 4実装開始前にAppleScriptを修正し、`osacompile`、`plutil -lint`、実行bit gateを通す必要がある。今回の担当範囲はdocs onlyのためコード修正は行っていない。
