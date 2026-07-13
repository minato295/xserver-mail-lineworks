# Mac Task 3 extension report: 共通 `record_python_runtime`

## 実装

- `macos.runtime.record_python_runtime(executable, version_info)` を公開した。
- Python version は exact な `tuple[int, int]` として検査し、bool を含む値と 3.13/3.14 以外を拒否する。
- executable は絶対 path、`Path.resolve(strict=True)` と同一の canonical path、非 symlink の通常 file に限定した。
- `lstat` の device/inode、canonical path、major/minor から `RuntimeRecord` を生成する。
- `validate_python_runtime()` と `validate_runtime_record()` も同じ version・recording/identity 規則を利用し、重複検証を除いた。
- 例外本文は固定 reason のみとし、path/version の入力値を含めない。

## TDD

- RED: 正常な 3.13/3.14、3.12/3.15、symlink/relative path、bool、device/inode、秘密非露出のテストを先に追加した。
- RED 確認: `python3 -m unittest tests.python.test_macos_runtime -v` は、未実装の `record_python_runtime` を import できず期待どおり失敗した。
- GREEN: 公開 API、共通 version validator、runtime identity helper を最小実装した。

## 検証

- `python3 -m unittest tests.python.test_macos_runtime tests.python.test_macos_launcher -v`: 19 tests、成功。
- `./tests/run-all.sh`: 108 tests、PHP checks、syntax checks、public secret scan、成功。
- `git diff --check`: 成功。

## 並行変更

- 担当外の `macos/local_config.py`、`manager/ftps_deployer.py`、`tests/python/test_ftps_deployer.py`、`tests/python/test_macos_local_config.py` の既存変更は保持し、このコミットへ含めない。

## Concerns

- なし。
