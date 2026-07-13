# Task 6 plan resolution report

## Scope

Task 6の監査指摘を設計書と実装計画だけで解消した。実装、Xserver、GitHub、Keychainは変更していない。並行中のTask 5未追跡fileも変更・stageしていない。

## Resolutions

1. bootstrapをComposer、autoload、release classへ依存しないstandalone固定private fileへ変更した。固定locator path、既存config pathを保持する単一locator schema、初回publish/readbackを明記した。初回後bootstrapはimmutableで、更新・valid-pair swapを行わない。
2. 旧layout移行を複数recipient/domain/ruleの本番filter集合transactionへ変更した。移行後のversion更新はfilter不変・locator単独切替えへ分離した。
3. remote validationは認証済みSSH aliasから直接PHP CLIを実行する方式へ簡素化した。一時validator filter、合成mail、nonce receipt/sidecarを削除した。
4. push前scanをHEADから到達可能な全commit tree、全parent/root patch、削除内容、tracked/untracked/ignored実値まで拡張した。
5. remote権限をdirectory `0700`、通常file/locator/config/log `0600`、bootstrap/実行PHP `0700`へexact固定した。SSH最小validator modeで`public_html`の非追従metadataと既知製品名だけを監査し、未知内容を読まない。
6. 限定ACL itemだけを持つ専用一時keychainでallow/deny promptを再現し、正常/拒否/許可/例外/SIGINTの全経路でfinally cleanup、残存0、本番件数・attribute・ACL・変更日時不変を要求した。
7. PR merge方式をmerge commitへ固定し、remote commitの親関係を検証する手順を追加した。
8. installer completion receiptをTDD前提へ追加し、非同期`open`終了だけを成功扱いしない手順へ変更した。
9. Task 5完了・clean・fresh review 0件gateを維持し、Task 6完了後も同じgateを通して統合Task 7へ進む構造にした。
10. Xserver APIは初回の本番managed filter集合移行/readbackだけに限定した。release upload/validation/switch中の一時filterとAPI mutationは0回とする。
11. ignored scanをNUL区切り・byte順の決定的列挙にし、nested repository/submodule/sparse/skip-worktree/unreadable/special file/TOCTOUをfail closedへ固定した。
12. bootstrapを初回移行後immutableへ変更し、通常更新は全release世代で後方互換な完全schemaのlocator単一atomic renameだけに限定した。各fault pointへ実メール起動を注入しold/new releaseのどちらか一回だけ、mixed観測0を要求した。bootstrap更新は別generation/pointer設計なしでは拒否する。
13. 初回filter移行は公式rule evaluation semanticsと実環境probeを必須にした。保証不能時はold/new同時存在を避ける短時間maintenanceへ切替え、LINE WORKS二重通知0とcopy mailbox原本消失0をE2Eする。mailbox取得権限がないため自動再投入・通知欠落0は保証せず、window中の通知停止を事前表示する。
14. bootstrap、locator、既存config、releases、transactionのremote absolute layout定数表を追加した。既存configは移動・複製せずlocatorの不変`config_path`で参照する。
15. Keychain testはglobal search/default変更を全面禁止し、`manager/keychain.py`のtest-only explicit temporary keychain path DIへ変更した。production pathなし、temp path一つだけ、全faultで一時物0・本番search/default/ACL不変をtestする。
16. FTPSに存在しないremote file/directory `fsync`保証を削除した。remote契約はsame-directory renameのatomic visibility、downloadしたbytes/hash/schema/modeのreadback、valid old/new recoveryだけとし、server crash durabilityはXserver基盤依存の制約としてdesign/README/planへ明記した。remote fault testから`fsync` mock/assertを除外し、Macローカル`fsync`契約と分離した。
17. 実接続成功済みのSSHを、具体接続値を一切文書化せずgeneric `ssh_alias`として利用する計画へ変更した。SSHはremote PHP CLI、manifest/mode/symlink/最小`public_html` metadata検証、FTPSはupload/download hash readback、APIは本番filter移行だけを担当する。既存5-key configはaliasだけを尋ねる6-key schemaへ明示migrationし、接続先/user/port/key pathは保存しない。
18. SSH argvを`/usr/bin/ssh`、固定`-o`、destination前`--`、検証済みalias、単一compile-time remote commandへ固定し、可変dataはstdin JSONだけにした。実`ssh -G` argv contract testを追加した。
19. SSH trust boundaryをcurrent UID・非symlink・owner-only config/known_hostsへ固定した。effective `ssh -G`のhostname/command/proxy/forward/canonicalize/known-hosts設定を秘密非出力で検査し、wildcard alias、host mismatch、unknown/changed key、parse/identity/mode異常で停止する。実行直前identity再検査と、OpenSSH path再open後のTOCTOUを完全には防げない制約も明記した。
20. simple fail-closedとしてmain SSH configの`Include`、全`Match`、全`HostKeyAlias`をraw parserで`ssh -G`前に拒否する。大小文字、global/全Host、indent、quote/comment/escape、odd-backslash継続、tokenization異常をnegative testへ追加した。actual argvから`HostKeyAlias=none`を削除し、resolved hostnameのknown_hosts entryでunknown/changedをactual SSH contract testする。main config/known_hostsは検査後とspawn直前にidentity/hash再照合する。

## Validation

- `git diff --check`: PASS
- placeholder scan: `TODO`/`TBD`なし
- 変更対象: design、plan、このreportだけ
