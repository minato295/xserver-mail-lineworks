# Task 5 preflight plan resolution report

## Scope

`docs/superpowers/plans/2026-07-11-macos-launcher-app.md` の Task 5 を再レビュー結果に基づき再設計した。コードとテスト実装は変更していない。配置先と権限境界が変わるため、設計書、planのGlobal Constraints、Task 4境界、Task 6検証pathも同期した。

## Resolved Critical findings

1. root bootstrap TOCTOUを解消するため、配置先を`~/Applications`へ変更した。installer/uninstallerは現在UIDだけで完結し、Authorization Services、SMAppService、sudo、privileged helperを一切使わない。
2. exact allowlist全項目の`dev/ino/uid/mode/size/hash` manifestと、`openat` + `O_NOFOLLOW`で同じfdからuser-private transactionへcopyする契約を維持した。
3. 更新は`renameatx_np(RENAME_SWAP)`、新規installは`RENAME_EXCL`を使い、unsupported filesystemでは非atomic fallbackをしない。
4. stateをcanonical payload checksum、単調generation、temp write + file fsync + atomic rename + directory fsyncへ具体化した。immutable generation pointerとstateの不一致、分岐、checksum不正、未知schemaでは何も削除せず停止する。
5. app/config transactionはcrash後にinstalled tree hashを照合し、newならcommit継続、oldならrollback、不明ならbackupを保持して停止する。
6. config renameより前に共通durable markerを作り、txn ID/generation、old/new config hash、backup dev/ino/hash、old/new app identity/hashを記録する契約へ変更した。config rename後・app PREPARED前のcrashはold appとの照合でconfigを確実にrollbackし、new appならforward commitする。
7. state確定後pointer確定前は単一highest orphan stateを検証してimmutable pointerを補完する正常回復とし、隔離しない。pointer rename後readback前も同様に再検証する。
8. config二段renameの`config欠落 + backup old + temp new`と、初回設定なしのtemp-only/config-publishedを明示的な状態行列へ追加し、app identityに応じold/oldまたはnew/newへ収束させる。
9. `CLEANED`後はparent directoryのdurable cleanup receiptにexact child identitiesを記録し、install/update/uninstallの段階削除を欠落許容・identity一致限定で再開してreceiptを最後に削除する。
10. 既存config (a) rename前、(b) rename間、(c) rename後と、初回config (d) temp-only、(e) publishedをapp absent/old/newと直積した15セルの判定表を追加した。各セルをold側/new側へidentity限定で収束し、表外・identity不一致は全bytes/inode不変で隔離する。`initial_temp_only + app=new`はtempをconfigへ公開する。
11. recovery自身の各mutation前に`RECOVER_<ACTION>` generationへexact pre/post snapshotsをdurable記録する。`NEW_TO_TRASH`直後の`config absent + backup old + trash new`を含む全中間形を正常post状態として認識し、次processが同じaction列を再開する。

## Resolved Important findings

- `~/Applications`内に現在UID owner `0700`の固定installer directory、`flock` lock、128-bit random transaction directoryを定義し、owner/mode/device/name衝突をfail closedにした。
- source内mount、hard link、symlink、FIFO、未知path、manifest identity差替え、既存appのowner不一致を拒否するtest要件を追加した。
- bundle IDをbuild、manifest、staging、installed validationの全境界で`jp.example.xserver-mail-lineworks-manager`へ固定し、planに残っていたローカルoverrideを削除した。
- uninstallをdurable `UNINSTALL_PREPARED` / `MOVED` / `CLEANED` transactionとtrash identity照合にし、crash後もforward completionできるようにした。config削除は一般ユーザー側、keychain削除は非対応のままとした。
- `renameatx_np`のctypes `argtypes`、`restype`、flag値、errno分類とSDK header照合testを明記した。
- ABI testをsymbol absent、NUL、`ENOENT`、`ENOTSUP`、`EINVAL`、`EXDEV`、unknown errno、fallback非呼出まで拡張し、`xcrun clang`でSDK headerの定数とfunction pointer signatureをcompile検証する契約にした。
- mutable `current`を廃止し、immutable `current.<generation>` pointerのrename後identity/content readbackとstateとの1対1検証へ変更した。
- `os.readlink` contract testはbytes pathと実`dir_fd`を渡してswap後targetを確認し、fake libcの`EEXIST`がcollisionへ分類されるtestを追加した。
- 全normal crash pointをworker processで発生させ、別recovery processがexit 0、非隔離、非mixed app/configで完了するtable-driven testを追加した。
- config/app全15組を別processで検証するmatrix testと、config/backup/temp/app identityを個別に改変してsnapshot bytes/inodes不変の隔離を検証するtestを追加した。
- `OLD_TO_BACKUP`、`PUBLISH_TEMP`、`NEW_TO_TRASH`、`RESTORE_BACKUP`、3種のdrop操作それぞれのrename/unlink+fsync直後に再crashし、さらに次のprocessが非隔離・非mixedへ収束するtestを追加した。
- focused test、macOS実syscall contract test、全回帰、secret scan、`git diff --check`をTask 5完了条件にした。

## Remaining concerns for implementation review

- `renameatx_np`の定数とctypes signatureは実macOS SDK/headerおよび実機contract testで照合する。symbol不存在、`ENOTSUP`、`EINVAL`、`EXDEV`は明示的に非対応として扱う。
- directory `fsync`のmacOS filesystem別挙動は実機で検証し、失敗を成功扱いにしない。
