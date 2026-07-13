# Xserverメール通知管理 Macアプリ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 既存の日本語管理CLIを、秘密情報を埋め込まずFinderからダブルクリック起動できる`Xserverメール通知管理.app`として安全に構築・インストールする。

**Architecture:** Python製インストーラーが安全なローカル設定を作成し、AppleScriptアプリバンドルへ既存管理CLI、固定シェルランチャー、Pythonランタイム検証情報、Python製起動ランタイムを同梱する。AppleScriptはTerminalへ固定ランチャーパスだけを渡し、起動ランタイムが設定をsymlink非追従で読み、固定したPython 3.13–3.14で管理CLIを子プロセス実行する。

**Tech Stack:** Python 3.13–3.14、標準ライブラリ、zsh、AppleScript/`osacompile`、macOS Terminal、unittest、既存PHP/Pythonテスト。

## Global Constraints

- 対応OSは現在利用中のmacOS、対応Pythonは3.13以上3.14以下。
- Pythonランタイムは同梱せず、インストーラーを実行した検証済みPythonのcanonical path・device・inodeを固定する。
- APIキーとFTPS認証情報はmacOSキーチェーン、Webhook URLはXserver非公開設定だけに保存する。
- SSH接続詳細はMacのSSH config/Keychainだけに保持し、アプリ設定には非秘密aliasだけを保存する。具体host/user/port/key pathはGit、docs、logへ記載しない。
- ユーザー設定は`~/Library/Application Support/XserverMailLineworks/config.json`、専用ディレクトリ`700`、ファイル`600`。
- `~/Library`と`~/Library/Application Support`は現在ユーザー所有、非symlink、group/other writableでないことを要求し、厳密な`700`は要求しない。
- AppleScriptがTerminalへ渡す動的値は、OS標準の`quoted form`で囲むアプリ内固定ランチャーパスだけとする。
- 配置先は現在ユーザー所有の`~/Applications/Xserverメール通知管理.app`へ固定し、appとtransaction領域は現在UID所有、非symlink、group/other writableでないことを要求する。
- `public_html`を含むパス、dot segment、改行、NUL、制御文字、未知JSONキーを拒否する。
- installer/uninstallerはsudo実行を拒否し、Authorization Services、`SMAppService`、privileged helper、管理者承認を一切使用しない。
- アプリはローカル生成の未署名バンドルとし、隔離属性の無効化は行わない。

---

### Task 1: 秘密非露出のトップレベルCLIエラー境界

**Files:**
- Modify: `manager/manage.py`
- Modify: `tests/python/test_manager.py`

**Interfaces:**
- Consumes: 既存`main() -> int`と`Keychain`、`XServerApi`、`FtpsDeployer`。
- Produces: `main()`がすべての初期化・実行例外を固定日本語メッセージへ変換し、秘密値とtracebackを出さず`2`を返す契約。

- [ ] **Step 1: 失敗テストを追加する**

`tests/python/test_manager.py`へ、APIキーチェーン読取、FTPSキーチェーン読取、予期しない例外が秘密値を表示しないテストを追加する。

```python
def test_main_redacts_all_startup_failures(self):
    environment = {
        "XSERVER_SERVERNAME": "server.example.invalid",
        "XSERVER_COMMAND_PATH": "/home/example/private/current/bin/mail-to-lineworks.php",
        "XSERVER_FTPS_HOST": "ftp.example.invalid",
        "XSERVER_CONFIG_PATH": "/mail-lineworks/private/config.json",
        "XSERVER_HOME": "/home/example",
    }
    stderr = io.StringIO()
    with patch.dict("os.environ", environment, clear=True), \
            patch("manager.keychain.Keychain.read_ftps_credentials",
                  side_effect=RuntimeError("secret-value")), \
            patch("sys.stderr", stderr):
        self.assertEqual(2, main())
    self.assertIn("認証情報または接続設定を確認できません", stderr.getvalue())
    self.assertNotIn("secret-value", stderr.getvalue())
    self.assertNotIn("Traceback", stderr.getvalue())
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `python3 -m unittest tests.python.test_manager.ManagerTest.test_main_redacts_all_startup_failures -v`

Expected: `RuntimeError: secret-value`が境界外へ出てFAIL。

- [ ] **Step 3: `main()`全体へ固定文言の例外境界を実装する**

`manager/manage.py`で既存本体を`_run_main()`へ移し、公開`main()`を次の形にする。

```python
def main():
    try:
        return _run_main()
    except (RuntimeError, ValueError, OSError):
        print("認証情報または接続設定を確認できません。キーチェーンと設定を確認してください。", file=sys.stderr)
        return 2
    except Exception:
        print("予期しないエラーで管理CLIを開始できませんでした。", file=sys.stderr)
        return 2
```

- [ ] **Step 4: focusedテストと既存テストを通す**

Run: `python3 -m unittest tests.python.test_manager -v`

Expected: 全ManagerテストPASS、標準エラーに秘密値・tracebackなし。

- [ ] **Step 5: コミットする**

```bash
git add manager/manage.py tests/python/test_manager.py
git commit -m "fix: redact management CLI startup failures"
```

---

### Task 2: 安全なローカル設定ローダー

**Files:**
- Create: `macos/__init__.py`
- Create: `macos/local_config.py`
- Create: `tests/python/test_macos_local_config.py`
- Modify: `tests/run-all.sh`

**Interfaces:**
- Produces: `validate_config(value: object) -> dict[str, str]`。設定ファイルI/Oを伴わず、Task 4の対話入力にも同じ完全schema検証を公開する。
- Produces: `load_config(path: pathlib.Path, uid: int) -> dict[str, str]`。
- Produces: `to_environment(config: dict[str, str]) -> dict[str, str]`。
- Throws: 内容を含まない`LocalConfigError`。

- [ ] **Step 1: スキーマ・権限・symlink・TOCTOU失敗テストを追加する**

```python
class LocalConfigTest(unittest.TestCase):
    def write_config(self, path, payload, mode=0o600):
        path.parent.mkdir(mode=0o700, parents=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(mode)
        return path

    def test_accepts_exact_schema_and_maps_environment(self):
        config = self.write_config(self.root / "XserverMailLineworks" / "config.json", {
            "servername": "server.example.invalid",
            "command_path": "/home/example/private/current/bin/mail.php",
            "ftps_host": "ftp.example.invalid",
            "config_path": "/mail-lineworks/private/config.json",
            "filesystem_home": "/home/example",
        })
        self.assertEqual("server.example.invalid", load_config(config, os.getuid())["servername"])

    def test_rejects_unknown_key(self):
        payload = dict(self.valid_payload, unknown="value")
        path = self.write_config(self.config_path, payload)
        with self.assertRaises(LocalConfigError):
            load_config(path, os.getuid())

    def test_rejects_file_symlink_and_loose_mode(self):
        real = self.write_config(self.root / "real.json", self.valid_payload)
        self.config_path.parent.mkdir(mode=0o700, parents=True)
        self.config_path.symlink_to(real)
        with self.assertRaises(LocalConfigError):
            load_config(self.config_path, os.getuid())
        self.config_path.unlink()
        path = self.write_config(self.config_path, self.valid_payload, mode=0o640)
        with self.assertRaises(LocalConfigError):
            load_config(path, os.getuid())

    def test_rejects_oversize(self):
        self.config_path.parent.mkdir(mode=0o700, parents=True)
        self.config_path.write_bytes(b"x" * 65537)
        self.config_path.chmod(0o600)
        with self.assertRaises(LocalConfigError):
            load_config(self.config_path, os.getuid())
```

- [ ] **Step 2: テストがモジュール不存在で失敗することを確認する**

Run: `python3 -m unittest tests.python.test_macos_local_config -v`

Expected: `ModuleNotFoundError: macos.local_config`。

- [ ] **Step 3: 非追従openと厳密バリデーションを実装する**

`macos/local_config.py`に以下を実装する。

```python
ENVIRONMENT_KEYS = {
    "servername": "XSERVER_SERVERNAME",
    "command_path": "XSERVER_COMMAND_PATH",
    "ftps_host": "XSERVER_FTPS_HOST",
    "config_path": "XSERVER_CONFIG_PATH",
    "filesystem_home": "XSERVER_HOME",
}

def load_config(path, uid):
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise LocalConfigError("設定ファイルが変更されました")
        body = os.read(fd, 65537)
    finally:
        os.close(fd)
    return validate_config(json.loads(body.decode("utf-8")))
```

内容検証は公開`validate_config(value: object) -> dict[str, str]`へ分離し、`load_config()`もJSON decode後に必ずこの公開APIを呼ぶ。Task 4はprivate名をimportしたり検証を複製したりしない。

親検査は`~/Library`と`Application Support`を現在ユーザー所有・非symlink・`mode & 0o022 == 0`、`XserverMailLineworks`を厳密`700`とする。

- [ ] **Step 4: 設定テストを通す**

Run: `python3 -m unittest tests.python.test_macos_local_config -v`

Expected: 正常・異常系すべてPASS。

- [ ] **Step 5: 一括テストへ新テストを組み込みコミットする**

Run: `bash tests/run-all.sh`

Expected: 既存48件と新規設定テスト、秘密情報スキャンがPASS。

```bash
git add macos tests/python/test_macos_local_config.py tests/run-all.sh
git commit -m "feat: validate macOS launcher configuration"
```

- [ ] **Step 6: Task 4が利用する公開validator契約の失敗テストを追加する**

`tests/python/test_macos_local_config.py`で`from macos.local_config import validate_config`し、正しいdictをcopyして返すこと、未知キー・不正hostname・`public_html`を含むpathを`LocalConfigError`で拒否することを直接検査する。

```python
def test_public_validate_config_uses_the_loader_schema(self):
    validated = validate_config(dict(self.valid_payload))
    self.assertEqual(self.valid_payload, validated)
    self.assertIsNot(self.valid_payload, validated)
    for invalid in (
        dict(self.valid_payload, unknown="value"),
        dict(self.valid_payload, servername="example.invalid"),
        dict(self.valid_payload, command_path="/home/example/public_html/manage.py"),
    ):
        with self.subTest(invalid=invalid), self.assertRaises(LocalConfigError):
            validate_config(invalid)
```

- [ ] **Step 7: 公開API未定義で失敗することを確認する**

Run: `python3 -m unittest tests.python.test_macos_local_config.LocalConfigTest.test_public_validate_config_uses_the_loader_schema -v`

Expected: `ImportError: cannot import name 'validate_config'`でFAIL。

- [ ] **Step 8: validatorを公開しloaderと共有する**

`macos/local_config.py`の`_validate_config(value: object)`を次の公開名へ変更し、`load_config()`末尾も`return validate_config(parsed)`へ変更する。

```python
def validate_config(value: object) -> dict[str, str]:
    """Validate and copy the exact non-secret local configuration schema."""
    if not isinstance(value, dict) or set(value) != set(KEY_TO_ENV):
        raise _fail("invalid configuration schema")
    for key, item in value.items():
        if not isinstance(item, str) or not item:
            raise _fail("invalid configuration value")
        if any(unicodedata.category(char) == "Cc" for char in item):
            raise _fail("invalid configuration value")
    if not _is_dns_hostname(value["servername"]) or not value["servername"].lower().endswith(
        (".xsrv.jp", ".xbiz.jp")
    ):
        raise _fail("invalid server name")
    if not _is_dns_hostname(value["ftps_host"]):
        raise _fail("invalid FTPS host")
    for key in ("command_path", "config_path", "filesystem_home"):
        if not _validate_path(value[key]):
            raise _fail("invalid path")
    return dict(value)
```

- [ ] **Step 9: 公開契約と全設定回帰を通してコミットする**

Run: `python3 -m unittest tests.python.test_macos_local_config -v`

Expected: 公開API直接テストと既存25件がすべてPASS。

```bash
git add macos/local_config.py tests/python/test_macos_local_config.py
git commit -m "refactor: expose macOS config validator"
```

---

### Task 3: 固定Python実体とTerminalランチャー

**Files:**
- Create: `macos/runtime.py`
- Create: `macos/launcher.sh`
- Create: `macos/AppLauncher.applescript`
- Create: `tests/python/test_macos_runtime.py`
- Create: `tests/python/test_macos_launcher.py`

**Interfaces:**
- Consumes: `load_config()`、`to_environment()`。
- Produces: `RuntimeRecord(path: str, device: int, inode: int, major: int, minor: int)` dataclass。
- Produces: `record_python_runtime(executable: str, version_info: tuple[int, int]) -> RuntimeRecord`。Task 4のinstallerと起動時検証が同じrecord型・対応版・canonical path規則を共有する。
- Produces: `validate_runtime_record(record: RuntimeRecord, stat_result: os.stat_result, version_info: tuple[int, int]) -> None`。
- Produces: `validate_python_runtime(metadata_path: Path, executable=sys.executable) -> RuntimeRecord`。
- Produces: `run_manager(resources_dir: Path, config_path: Path) -> int`。
- `python-runtime.json`形式: `{"path": str, "device": int, "inode": int, "major": 3, "minor": 13|14}`。

- [ ] **Step 1: Python版・inode・秘密非露出・終了コードの失敗テストを追加する**

```python
def test_runtime_rejects_python_312_and_changed_inode(self):
    with self.assertRaises(RuntimeError):
        validate_runtime_record(record(minor=12), current_stat)
    with self.assertRaises(RuntimeError):
        validate_runtime_record(record(inode=1), current_stat)

def test_manager_receives_environment_without_shell(self):
    result = run_manager(resources, config_path, runner=fake_runner)
    self.assertEqual(7, result)
    self.assertEqual("server.example.invalid", fake_runner.env["XSERVER_SERVERNAME"])

def test_record_python_runtime_is_shared_with_installer(self):
    record = record_python_runtime(str(self.executable), (3, 13))
    self.assertEqual(str(self.executable.resolve()), record.path)
    self.assertEqual((self.info.st_dev, self.info.st_ino), (record.device, record.inode))
    with self.assertRaises(RuntimeError):
        record_python_runtime(str(self.executable), (3, 12))
```

- [ ] **Step 2: テストが実装不存在で失敗することを確認する**

Run: `python3 -m unittest tests.python.test_macos_runtime tests.python.test_macos_launcher -v`

Expected: runtime/launcher関数未定義でFAIL。

- [ ] **Step 3: Python runtimeと固定起動を実装する**

`runtime.py`は`subprocess.run([sys.executable, manager_path], env=env, check=False).returncode`だけで管理CLIを実行し、shellを使わない。`launcher.sh`はアプリ内`python-runtime.json`からPythonパスを抽出せず、インストール時に生成する固定1行だけの`python-path`を`IFS= read -r PYTHON_EXECUTABLE < "$RESOURCES/python-path"`で読み、絶対パス・改行なしを検査後`"$PYTHON_EXECUTABLE" "$RESOURCES/runtime.py"`を実行する。`runtime.py`が同じResources内の`python-runtime.json`に記録したcanonical path・device・inode・版との完全一致を再検査する。

`record_python_runtime()`は絶対かつcanonicalな通常ファイルだけを`lstat`し、symlinkとPython 3.12以下/3.15以上を拒否して`RuntimeRecord`を返す。Task 4の`validate_installer_python()`はこの関数を呼ぶだけとし、runtime metadataの生成規則を複製しない。

AppleScriptは、実macOSの`osacompile`で利用できない`path to resource`を使わず、`path to me`から自身のbundleを取得し、固定suffix`Contents/Resources/launcher.sh`だけを連結する。Terminalの`do script`相当は辞書依存を避けるため標準Apple event `core/doex`を用い、責務を次だけに限定する。

```applescript
on run
  set appPath to POSIX path of (path to me)
  set launcherPath to appPath & "Contents/Resources/launcher.sh"
  tell application "Terminal"
    activate
    «event coredoex» quoted form of launcherPath
  end tell
end run
```

Terminal内の`launcher.sh`は終了コードを日本語表示し、`read -r`でEnterを待って同じ終了コードで終了する。

- [ ] **Step 4: runtime・AppleScript静的安全テストを通す**

Run: `python3 -m unittest tests.python.test_macos_runtime tests.python.test_macos_launcher -v`

Expected: Python 3.12拒否、3.13/3.14受理、inode変更拒否、shell未使用、AppleScriptに設定キーなしでPASS。

- [ ] **Step 5: 実macOS compilerをTask 4開始前gateとして通す**

Run:

```bash
rm -rf /tmp/XserverMailManager-task3.app
osacompile -o /tmp/XserverMailManager-task3.app macos/AppLauncher.applescript
plutil -lint /tmp/XserverMailManager-task3.app/Contents/Info.plist
test -x /tmp/XserverMailManager-task3.app/Contents/MacOS/applet
```

Expected: `osacompile` exit 0、`plutil`が`OK`、`test -x` exit 0。compiler errorが1件でもあればTask 3未完了としてAppleScriptを修正し、このgateが通るまでTask 4へ進まない。

- [ ] **Step 6: コミットする**

```bash
git add macos/runtime.py macos/launcher.sh macos/AppLauncher.applescript tests/python/test_macos_runtime.py tests/python/test_macos_launcher.py
git commit -m "feat: add secure Terminal app launcher"
```

---

### Task 4: 一般ユーザー権限でのアプリ構築・設定transaction

**Files:**
- Create: `macos/install_app.py`
- Create: `macos/install_app.command`
- Create: `tests/python/test_macos_installer.py`

**Interfaces:**
- Consumes: Task 2の`validate_config(value: object) -> dict[str, str]`、Task 3の`RuntimeRecord`と`record_python_runtime(executable: str, version_info: tuple[int, int]) -> RuntimeRecord`。
- Produces: `validate_installer_python(version_info: tuple[int, int], executable: str) -> RuntimeRecord`。実装は`record_python_runtime()`を共有する。
- Produces: `ConfigTransaction.commit() -> None`、`ConfigTransaction.rollback() -> None`。
- Produces: `write_config_atomic(path: Path, values: object, uid: int, transaction: ConfigTransactionSpec | None = None) -> ConfigTransaction`。Task 4単体では`None`を許す。Task 5は共通txn IDと予測不能backup basenameを持つspecを必ず渡し、返却時点では新設定がdurableに配置済み、配置成功時に`commit()`、失敗時に`rollback()`する。
- Produces: `build_bundle(source_root, build_root, runtime_record) -> Path`。
- Produces: `validate_bundle(bundle: Path, source_root: Path, uid: int) -> None`。exact allowlist、bundle ID、owner/mode、秘密走査を一括検査する。
- Task 4は`~/Applications`へ書かず、`install_bundle`も定義しない。一般ユーザー権限での配置・更新はTask 5だけが所有する。

- [ ] **Step 1: runtime共有、sudo拒否、dirfd設定transaction、source/build trustの失敗テストを追加する**

```python
def test_installer_rejects_sudo_and_python_312(self):
    with self.assertRaises(InstallError):
        ensure_unprivileged(real_uid=501, effective_uid=0)
    with self.assertRaises(InstallError):
        validate_installer_python((3, 12), "/usr/bin/python3")

def test_existing_config_is_not_overwritten_without_exact_confirmation(self):
    self.assertFalse(write_config_with_confirmation(path, new, answer="いいえ"))
    self.assertEqual(old_bytes, path.read_bytes())

def test_atomic_config_rollback_restores_old_bytes(self):
    transaction = write_config_atomic(self.config_path, self.new_values, os.getuid())
    self.assertEqual(self.new_values, load_config(self.config_path, os.getuid()))
    transaction.rollback()
    self.assertEqual(self.old_bytes, self.config_path.read_bytes())

def test_bundle_contains_exact_runtime_package_and_generated_files(self):
    bundle = build_bundle(self.source_root, self.build_root, self.runtime_record)
    self.assertEqual(EXPECTED_BUNDLE_FILES, relative_bundle_files(bundle))
    for required in (
        "Contents/Resources/runtime.py",
        "Contents/Resources/local_config.py",
        "Contents/Resources/launcher.sh",
        "Contents/Resources/python-path",
        "Contents/Resources/python-runtime.json",
        "Contents/Resources/manager/manage.py",
    ):
        self.assertIn(required, EXPECTED_BUNDLE_FILES)

def test_rejects_untrusted_source_and_build_roots(self):
    self.source_root.chmod(0o777)
    with self.assertRaises(InstallError):
        build_bundle(self.source_root, self.build_root, self.runtime_record)
```

- [ ] **Step 2: テストが実装不存在で失敗することを確認する**

Run: `python3 -m unittest tests.python.test_macos_installer -v`

Expected: `ModuleNotFoundError`または`ConfigTransaction`、`build_bundle`未定義でFAIL。

- [ ] **Step 3: 固定候補だけを使うダブルクリックbootstrapを実装する**

`install_app.command`は自身をsymlink非追従で検査し、`cd -P -- "$(dirname -- "$0")"`で固定source rootを得る。次の候補をこの順序でだけ検査し、`PATH`検索、`command -v`、`which`、`/usr/bin/env python3`、任意の環境変数による候補追加を禁止する。

```text
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
/opt/homebrew/bin/python3.14
/usr/local/bin/python3.14
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
/opt/homebrew/bin/python3.13
/usr/local/bin/python3.13
```

各候補は通常ファイル・非symlinkを確認してから`-I -c`で版を検査し、最初の適合Pythonを`-I "$SOURCE_ROOT/macos/install_app.py" --source-root "$SOURCE_ROOT"`の固定argvで起動する。候補がなければPython公式installerまたはHomebrewで3.13/3.14を導入する固定日本語案内を表示し、TTY時だけEnterを待って非0終了する。

- [ ] **Step 4: dirfd相対のdurable設定transactionを実装する**

入力dictは最初にTask 2の公開`validate_config(values)`へ渡す。HOMEから`Library`、`Application Support`を`O_DIRECTORY | O_NOFOLLOW`で順にopenし、所有者が`uid`、group/other non-writableであることを検査する。`XserverMailLineworks`はsupport dirfd相対で`0700`作成または非追従openし、所有者`uid`・mode `0700`を要求する。

app dirfd内で予測不能な一時名を`O_CREAT | O_EXCL | O_NOFOLLOW`, `0600`で作り、canonical JSON bytesを書き、file `fsync`、`fchmod(0600)`後に既存`config.json`を同じdirfd内の予測不能backup名へ`os.rename(..., src_dir_fd=app_fd, dst_dir_fd=app_fd)`する。次にtempを`config.json`へdirfd相対renameし、app directoryを`fsync`する。path文字列に対する`Path.write_text`、`os.replace`、tempfileの別directory利用は禁止する。

`ConfigTransaction.rollback()`は新`config.json`をdirfd相対で削除し、backupを元名へrenameしてdirectory `fsync`する。新規設定なら新fileだけを削除してdirectory `fsync`する。`commit()`はbackupを削除してdirectory `fsync`する。どちらも一度だけ呼べ、処理後に全fdを閉じる。作成途中の例外はtempを削除し、既存設定を元名へ戻し、directory `fsync`してから再送出する。

- [ ] **Step 5: source/build trustとbundle構築を実装する**

`install_app.py`は次を行う。

1. root実行を拒否。
2. source rootをcanonical absolute directoryとして固定し、root directoryは現在uid所有・非symlink・group/other non-writableを要求する。`macos/AppLauncher.applescript`、`macos/runtime.py`、`macos/local_config.py`、`macos/launcher.sh`、manager 4ファイルは各々`lstat`し、現在uid所有の通常ファイル・非symlink・group/other non-writableを要求し、検査後openしたfdのdevice/inode/uid/mode/typeが一致することを確認してfdからcopyする。
3. build rootは現在uid所有・非symlink・mode `0700`の新規一時directoryだけを許可し、既存pathやsource root配下、system/userいずれの`Applications`配下も拒否する。
4. 現在PythonをTask 3の`record_python_runtime()`で記録する。
5. build rootへ`osacompile -o Xserverメール通知管理.app macos/AppLauncher.applescript`をshellなし固定argvで実行する。
6. `Contents/Resources/manager/`へ`manage.py`、`keychain.py`、`ftps_deployer.py`、`xserver_api.py`、Resources直下へ`runtime.py`、`local_config.py`、`launcher.sh`、`python-path`、`python-runtime.json`を配置する。`local_config.py`を欠くbundleは必ず拒否する。
7. `Info.plist`の`CFBundleIdentifier`を`jp.example.xserver-mail-lineworks-manager`へ固定する。Task 5のmanifest・配置済みapp検証と同じ定数を共有し、環境変数、引数、ローカル設定による上書きを許可しない。

- [ ] **Step 6: exact allowlistと生成物対応のbundle検証を実装する**

`validate_bundle()`は`osacompile`生成後の相対file一覧を取得し、compiler生成物とTask 4追加物を合わせた次の集合との完全一致を要求する。globや「Contents以下すべて」は許可しない。

```python
EXPECTED_BUNDLE_FILES = {
    "Contents/Info.plist",
    "Contents/MacOS/applet",
    "Contents/PkgInfo",
    "Contents/Resources/Scripts/main.scpt",
    "Contents/Resources/applet.rsrc",
    "Contents/Resources/launcher.sh",
    "Contents/Resources/local_config.py",
    "Contents/Resources/runtime.py",
    "Contents/Resources/python-path",
    "Contents/Resources/python-runtime.json",
    "Contents/Resources/manager/manage.py",
    "Contents/Resources/manager/keychain.py",
    "Contents/Resources/manager/ftps_deployer.py",
    "Contents/Resources/manager/xserver_api.py",
}
```

directoryとfileを非追従walkし、symlink、socket、device、未知file/dir、現在uid以外のowner、group/other writableを拒否する。`Contents/MacOS/applet`と`launcher.sh`だけに実行bitを要求する。`plutil -lint`、`CFBundleIdentifier`完全一致、`python-path`とruntime record一致を確認する。

秘密scannerはsource tree用既存scannerだけに依存せず、生成済みbundleの全regular fileを対象にする。text fileは既知Webhook/API key/実hostname/実mail/秘密設定名を走査し、binaryの`main.scpt`、`applet.rsrc`、`Contents/MacOS/applet`もbyte列で同じASCII/UTF-8 tokenを走査する。scanner自身のfixture tokenはbundleへ入れず、検出時の例外には一致内容を含めない。

- [ ] **Step 7: インストーラーfocusedテストを通す**

Run: `python3 -m unittest tests.python.test_macos_installer -v`

Expected: runtime共有、dirfd相対atomic write、file/directory fsync、rollback、source/build拒否、bundle exact allowlist、`local_config.py`同梱、生成binary秘密走査がすべてPASS。

- [ ] **Step 8: 全回帰とdiff検査後にコミットする**

Run: `bash tests/run-all.sh`

Run: `git diff --check`

Expected: 既存全suiteとpublic secret scanがPASS、`git diff --check`は出力なし。

```bash
git add macos/install_app.py macos/install_app.command tests/python/test_macos_installer.py
git commit -m "feat: build macOS management app"
```

---

### Task 5: 安全な配置・更新・アンインストール

**Files:**
- Create: `macos/install_transaction.py`
- Create: `macos/uninstall_app.py`
- Modify: `macos/install_app.py`
- Modify: `tests/python/test_macos_installer.py`
- Create: `tests/python/test_macos_uninstaller.py`

**Interfaces:**
- Consumes: Task 4の`validate_bundle()`と`ConfigTransaction`。
- Produces: `BundleManifest(bundle_id: str, source_root: FileIdentity, directories: tuple[ManifestDirectory, ...], files: tuple[ManifestFile, ...], tree_sha256: str)`。`FileIdentity`は`dev: int, ino: int, uid: int, mode: int`、file項目はさらに`size: int, sha256: str`を持つ。相対pathはTask 4のexact allowlistだけを許す。
- Produces: `create_bundle_manifest(bundle: Path, uid: int) -> BundleManifest`、`copy_manifest_tree(manifest: BundleManifest, source_fd: int, transaction_fd: int) -> InstalledIdentity`。
- Produces: `rename_swap(src_dir_fd: int, src: bytes, dst_dir_fd: int, dst: bytes) -> None`、`rename_exclusive(src_dir_fd: int, src: bytes, dst_dir_fd: int, dst: bytes) -> None`。
- Produces: `recover_transactions(applications_fd: int, uid: int) -> RecoveryOutcome`。install、update、uninstallの確定stateをchecksum/generation順に照合し、既知identityだけをforward completionまたはrollbackする。
- Produces: `InstallTransaction(txn_id: str, generation: int, old_config_hash: str | None, new_config_hash: str, backup_identity: FileIdentity | None, old_app: InstalledIdentity | None, new_app: InstalledIdentity)`。configとappのstateは同じ128-bit `txn_id`を必須とする。
- Produces: `install_bundle(bundle: Path, destination: Path, uid: int) -> InstallOutcome`。`InstallOutcome`は`NEW_INSTALLED`または`UPDATED`で、一般ユーザーprocess内で再検証・manifest生成・atomic公開を行う。
- Produces: `install_with_config(source_root: Path, build_root: Path, config_values: object, uid: int) -> None`。build → bundle検証 → 設定transaction配置 → app配置 → 設定commitの順序を固定し、app配置失敗時は設定rollbackする。
- Produces: `validate_existing_app(path: Path, allowed_uid: int) -> InstalledIdentity`。固定path、bundle ID、全同梱物、owner/mode、mount境界を非追従検証する。
- Produces: `uninstall(remove_config: bool = False, remove_keychain: bool = False) -> None`。`remove_keychain=True`は常に拒否し、キーチェーンは別手順とする。
- `destination`はHOMEを非追従openして得たdirfd chain相対の`Applications/Xserverメール通知管理.app`だけを許す。absolute path文字列の比較だけに依存せず、HOME・Applications・appのownerをすべて`uid`へ固定する。

- [ ] **Step 1: manifest固定、非追従copy、atomic公開、回復、uninstall分離の失敗テストを追加する**

```python
def test_manifest_records_every_allowed_inode_and_rejects_source_replacement(self):
    manifest = create_bundle_manifest(self.new_bundle, os.getuid())
    replace_with_same_bytes(self.new_bundle / "Contents/Resources/launcher.sh")
    with self.assertRaisesRegex(InstallError, "検証後に変更"):
        install_bundle_from_manifest(manifest)

def test_transaction_copy_opens_exact_allowlist_without_following_links(self):
    manifest = create_bundle_manifest(self.new_bundle, os.getuid())
    replace_with_symlink(self.new_bundle / "Contents/Resources/runtime.py", self.outside)
    with self.assertRaises(InstallError):
        copy_manifest_tree(manifest, self.transaction_stage_fd)
    self.assertFalse((self.transaction_stage / "Xserverメール通知管理.app").exists())

def test_update_uses_rename_swap_and_failure_keeps_old_bundle(self):
    with self.assertRaises(InstallError):
        publish_update(self.staged_new, self.installed_app, rename_swap=failing_swap)
    self.assertEqual("old", read_marker(self.installed_app))

def test_recovery_swaps_old_bundle_back_after_post_swap_validation_failure(self):
    publish_update(self.staged_new, self.installed_app, validator=failing_validator)
    self.assertEqual("old", read_marker(self.installed_app))

def test_new_install_uses_noreplace_rename_and_never_overwrites_collision(self):
    make_app_at(self.installed_app, marker="unexpected")
    with self.assertRaises(InstallError):
        publish_new(self.staged_new, self.installed_app)
    self.assertEqual("unexpected", read_marker(self.installed_app))

def test_uninstall_defaults_to_app_only(self):
    uninstall(remove_config=False, remove_keychain=False)
    self.assertFalse(app.exists())
    self.assertTrue(config.exists())
    self.assertTrue(keychain_untouched)

def test_installer_never_requests_privilege_and_rejects_wrong_owner(self):
    self.assertNotIn("administrator privileges", installer_source())
    self.assertNotIn("sudo", installer_argv())
    with self.assertRaises(InstallError):
        validate_existing_app(self.other_user_owned_fake_app, os.getuid())

def test_install_failure_rolls_back_configuration(self):
    with self.assertRaises(InstallError):
        install_with_config(
            self.source_root, self.build_root, self.new_values, os.getuid()
        )
    self.assertEqual(self.old_config_bytes, self.config_path.read_bytes())
    self.assertEqual("old", read_marker(self.installed_app))

def test_crash_after_swap_recovers_forward_when_new_manifest_is_installed(self):
    simulate_install_crash(self.transaction, point="after_swap_before_state_replace")
    recover_transactions(self.applications_fd, os.getuid())
    self.assertEqual(self.new_config_bytes, self.config_path.read_bytes())
    self.assertEqual(self.new_tree_sha256, installed_tree_sha256(self.installed_app))

def test_corrupt_highest_generation_stops_without_deleting_either_bundle(self):
    corrupt_checksum(self.transaction / "state.7.json")
    with self.assertRaisesRegex(RecoveryError, "transactionを隔離"):
        recover_transactions(self.applications_fd, os.getuid())
    self.assertTrue(self.installed_app.exists())
    self.assertTrue(self.staged_or_old_app.exists())

def test_uninstall_crash_after_trash_rename_finishes_forward(self):
    simulate_uninstall_crash(self.transaction, point="after_rename_before_moved_state")
    recover_transactions(self.applications_fd, os.getuid())
    self.assertFalse(self.installed_app.exists())
    self.assertFalse(self.transaction_trash.exists())

def test_crash_after_config_rename_before_app_prepared_rolls_config_back(self):
    simulate_install_crash(self.transaction, point="after_config_rename_before_app_prepared")
    recover_transactions(self.applications_fd, os.getuid())
    self.assertEqual(self.old_config_hash, sha256_file(self.config_path))
    self.assertEqual(self.old_app_hash, installed_tree_sha256(self.installed_app))

def test_config_and_app_state_must_share_transaction_id(self):
    rewrite_app_state(txn_id="different-transaction")
    with self.assertRaisesRegex(RecoveryError, "transactionを隔離"):
        recover_transactions(self.applications_fd, os.getuid())

def test_committed_state_without_generation_pointer_is_completed_normally(self):
    simulate_install_crash(self.transaction, point="after_state_rename_before_pointer")
    result = recover_in_fresh_process(self.transaction)
    self.assertEqual("recovered", result.status)
    self.assertTrue((self.transaction / "current.2").exists())
    self.assertFalse(result.quarantined)

def test_existing_config_middle_rename_rolls_back_before_app_prepare(self):
    simulate_install_crash(self.transaction, point="config_missing_backup_old_temp_new")
    result = recover_in_fresh_process(self.transaction)
    self.assertEqual(self.old_config_hash, result.config_hash)
    self.assertEqual(self.old_app_hash, result.app_hash)
    self.assertFalse(result.quarantined)

def test_first_config_publish_converges_with_app_state(self):
    for point in ("initial_temp_only", "initial_config_published"):
        for app_state in ("absent", "old", "new"):
            with self.subTest(point=point, app_state=app_state):
                result = recover_initial_config_in_fresh_process(point, app_state)
                expected = (self.new_config_hash, self.new_app_hash) \
                    if app_state == "new" else (None, self.old_app_hash_or_none(app_state))
                self.assertEqual(expected, (result.config_hash, result.app_hash))
                self.assertFalse(result.quarantined)

CONFIG_RECOVERY_CASES = {
    # (config state, app state): expected public pair
    **{(state, "absent"): ("old", "absent") for state in ("a", "b", "c")},
    **{(state, "old"): ("old", "old") for state in ("a", "b", "c")},
    **{(state, "new"): ("new", "new") for state in ("a", "b", "c")},
    ("d", "absent"): ("absent", "absent"),
    ("d", "old"): ("absent", "old"),
    ("d", "new"): ("new", "new"),
    ("e", "absent"): ("absent", "absent"),
    ("e", "old"): ("absent", "old"),
    ("e", "new"): ("new", "new"),
}

def test_complete_config_app_matrix_recovers_in_fresh_process(self):
    for (config_state, app_state), expected in CONFIG_RECOVERY_CASES.items():
        with self.subTest(config_state=config_state, app_state=app_state):
            fixture = create_matrix_fixture(config_state, app_state)
            result = fixture.run_recovery_process()
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(expected, fixture.public_state())
            fixture.assert_no_mixed_pair()
            self.assertNotIn("隔離", result.stderr)

def test_unmatched_matrix_identity_quarantines_without_any_mutation(self):
    for mutation in ("config_inode", "backup_hash", "temp_mode", "app_tree_hash"):
        with self.subTest(mutation=mutation):
            fixture = create_matrix_fixture("b", "new")
            fixture.mutate_identity(mutation)
            before = fixture.snapshot_all_bytes_and_inodes()
            result = fixture.run_recovery_process()
            self.assertNotEqual(0, result.returncode)
            self.assertIn("隔離", result.stderr)
            self.assertEqual(before, fixture.snapshot_all_bytes_and_inodes())

RECOVERY_ACTIONS = (
    "OLD_TO_BACKUP", "PUBLISH_TEMP", "NEW_TO_TRASH", "RESTORE_BACKUP",
    "DROP_TEMP", "DROP_BACKUP", "DROP_TRASH",
)

def test_crash_after_every_recovery_mutation_is_recovered_by_next_process(self):
    for action in RECOVERY_ACTIONS:
        with self.subTest(action=action):
            fixture = create_fixture_reaching_recovery_action(action)
            first = fixture.run_recovery_process(crash_after=f"{action}:mutation_fsync")
            self.assertEqual(CRASH_EXIT_CODE, first.returncode)
            intermediate = fixture.snapshot_all_bytes_and_inodes()
            self.assertEqual(fixture.expected_post_snapshot(action), intermediate)
            second = fixture.run_recovery_process()
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertNotIn("隔離", second.stderr)
            self.assertIn(fixture.public_hash_pair(), {
                fixture.old_hash_pair, fixture.new_hash_pair,
            })
            fixture.assert_no_mixed_pair()

def test_crash_during_cleaned_deletion_resumes_by_recorded_identity(self):
    simulate_install_crash(self.transaction, point="after_cleaned_delete_third_entry")
    result = recover_in_fresh_process(self.transaction)
    self.assertFalse(result.transaction_exists)
    self.assertFalse(result.cleanup_receipt_exists)
    self.assertFalse(result.quarantined)
```

同じtest moduleに、source file/dirの`dev`、`ino`、`uid`、mode、size、hashの各不一致、未知path、hard link、FIFO、symlink、source内mount、`~/Applications`と異なるdevice、既存appのownerが現在UIDでない場合、固定bundle ID不一致、transaction/lock/stateのsymlink・owner・mode不一致、staging名衝突、共通marker作成前後、config backup rename前後、new config rename前後、app `PREPARED`確定前後、swap前後、各state確定前後、config commit前後の全crash point、state checksum/generation/txn ID破損、directory `fsync`失敗、固定destination以外を個別testにする。`sudo`、`osascript ... administrator privileges`、Authorization Services、SMAppServiceが一度も呼ばれないこともmockでassertする。

- [ ] **Step 2: テストが失敗することを確認する**

Run: `python3 -m unittest tests.python.test_macos_installer tests.python.test_macos_uninstaller -v`

Expected: manifest、`renameatx_np` wrapper、atomic state、install/uninstall recovery関数未定義でFAIL。

- [ ] **Step 3: 一般ユーザー側でimmutable manifestと固定配置境界を実装する**

一般ユーザー側`install_bundle()`はTask 4の`validate_bundle()`を配置直前に再実行する。その非追従walkと同じopen済みfdから、bundle root、許可directory、許可regular fileごとの相対path、`st_dev`、`st_ino`、`st_uid == uid`、型を除くpermission bits、file size、SHA-256をcanonical JSON manifestへ記録する。rootと全項目は同じ`st_dev`、link count 1、group/other non-writableを要求し、hard link、mount crossing、symlink、未知pathを拒否する。bundle IDはローカルoverrideを認めず、Task 4の生成・検証・manifest・配置済み検証のすべてで`jp.example.xserver-mail-lineworks-manager`に固定する。

manifestはbuild root内に`0600`, `O_CREAT|O_EXCL|O_NOFOLLOW`で作成し、fileと親directoryを`fsync`する。HOMEを`O_DIRECTORY|O_NOFOLLOW`で開いて`st_uid == uid`、group/other non-writableを要求する。`Applications`は不在ならdirfd相対に`0700`で作成し、既存なら現在UID所有・非symlink・group/other non-writableを要求してopenする。destinationはそのdirfd相対の固定basenameだけとする。全処理は現在のPython process内で行い、shell、AppleScript、Authorization Services、SMAppService、別UID processを介さない。

- [ ] **Step 4: user private transactionへexact allowlistをfd相対copyする**

`~/Applications/.xserver-mail-lineworks-installer`をApplications dirfd相対にmode `0700`で作成またはopenし、現在UID所有、同一device、非symlinkを要求する。その中の永続`lock`を`O_RDWR|O_CREAT|O_NOFOLLOW`, `0600`で開き、現在UID owner・regular file・link count 1を確認して`flock(LOCK_EX|LOCK_NB)`する。transaction directoryは128-bit乱数名を`mkdirat(..., 0700)`し、衝突時は新しい乱数で最大16回だけ再試行し、既存entryを開かず削除しない。

source bundleは親から各segmentを`openat(O_DIRECTORY|O_NOFOLLOW)`して開く。manifestのdirectoryを親dirfd相対、fileを`openat(O_RDONLY|O_NOFOLLOW)`で一項目ずつ開き、copy直前の`fstat`がmanifestの`dev/ino/uid/mode/size/type`と一致することを確認し、同じfdからEOFまで読みながらSHA-256を再計算する。destinationはtransaction dirfd配下にexact directoryを`mkdirat`、fileを`openat(O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, manifest.mode & 0o755)`してcopyし、`fchmod`、file `fsync`を行う。`ditto`、`cp -R`、`copytree`、glob、再帰walkは禁止する。全file後にleafからrootへdirectoryを`fsync`し、現在UID所有、exact allowlist、固定bundle ID、mode、主要実行bit、tree hashをstaging側fdだけで再検証する。

- [ ] **Step 5: `RENAME_SWAP`更新、新規rename、durable state recoveryを実装する**

`install_transaction.py`のDarwin境界だけが`ctypes.CDLL(None, use_errno=True).renameatx_np`を使う。ABIを`argtypes=[c_int,c_char_p,c_int,c_char_p,c_uint]`, `restype=c_int`へ固定し、filesystem encoding後にNULを含まないbytesだけを渡す。return `-1`なら他のctypes/Python syscallより先に`ctypes.get_errno()`を保存し、`EEXIST`はcollision、`ENOENT`はrace、`ENOTSUP`/`EINVAL`はunsupported flag/filesystem、`EXDEV`はmount逸脱、その他はerrno番号やpathを表示しないunknown syscall errorへ分類する。symbol不存在、NUL、全errnoで二段rename、`os.rename`、copy/delete fallbackを呼ばない。

SDK contractは`xcrun --sdk macosx --show-sdk-path`で得たSDKだけを使い、test tempの次のC sourceを`xcrun clang -std=c11 -Werror -isysroot "$SDKROOT" -c renameatx_contract.c -o renameatx_contract.o`でcompileする。repositoryへSDK pathや生成objectを保存しない。

```c
#include <stdio.h>
#include <sys/types.h>
_Static_assert(RENAME_SWAP == 0x00000002, "unexpected RENAME_SWAP");
_Static_assert(RENAME_EXCL == 0x00000004, "unexpected RENAME_EXCL");
typedef int (*expected_renameatx_np)(int, const char *, int, const char *, unsigned int);
static expected_renameatx_np signature_check = &renameatx_np;
int use_signature(void) { return signature_check != 0; }
```

```python
class RenameAtxContractTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS syscall contract")
    def test_swap_and_excl_match_darwin_behavior(self):
        self.assertEqual(0x00000002, RENAME_SWAP)
        self.assertEqual(0x00000004, RENAME_EXCL)
        os.symlink(b"OLD", b"old", dir_fd=self.dir_fd)
        os.symlink(b"NEW", b"new", dir_fd=self.dir_fd)
        rename_swap(self.dir_fd, b"old", self.dir_fd, b"new")
        self.assertEqual(b"NEW", os.readlink(b"old", dir_fd=self.dir_fd))
        with self.assertRaisesRegex(InstallError, "collision"):
            rename_exclusive(self.dir_fd, b"old", self.dir_fd, b"new")

    def test_fake_libc_eexist_is_classified_as_collision(self):
        with self.assertRaisesRegex(InstallError, "collision"):
            rename_exclusive(3, b"old", 3, b"new",
                             libc=FailingRenameAtx(errno.EEXIST))

    def test_errno_is_captured_before_any_other_ctypes_call(self):
        fake = FailingRenameAtx(errno.EXDEV)
        with self.assertRaisesRegex(InstallError, "mount"):
            rename_swap(3, b"a", 4, b"b", libc=fake)
        self.assertEqual([ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint], fake.argtypes)

    def test_all_failures_are_fail_closed_without_fallback(self):
        cases = (
            (errno.ENOENT, "race"), (errno.ENOTSUP, "unsupported"),
            (errno.EINVAL, "unsupported"), (errno.EXDEV, "mount"),
            (errno.EIO, "syscall"),
        )
        for error_number, message in cases:
            with self.subTest(error_number=error_number), \
                    patch("os.rename") as fallback, \
                    self.assertRaisesRegex(InstallError, message):
                rename_swap(3, b"a", 3, b"b", libc=FailingRenameAtx(error_number))
            fallback.assert_not_called()

    def test_absent_symbol_and_nul_are_rejected_without_fallback(self):
        with patch("os.rename") as fallback:
            with self.assertRaisesRegex(InstallError, "利用できません"):
                load_renameatx(libc=LibraryWithoutRenameAtx())
            with self.assertRaisesRegex(ValueError, "NUL"):
                rename_swap(3, b"a\0suffix", 3, b"b")
        fallback.assert_not_called()
```

既存appは現在UID所有、Applicationsと同じdevice、固定bundle ID、exact allowlist、group/other non-writableを非追従検証する。state payloadは`schema=1`、共通`txn_id`、単調増加`generation`、phase、old/new config hash、backup identity/hash、old/new app identity/hash、staged/destination nameを持ち、canonical JSON payloadのSHA-256を`checksum`として包む。更新ごとに`state.<generation>.tmp`を`O_EXCL|O_NOFOLLOW,0600`で作成し、write、file `fsync`、`renameatx_np(RENAME_EXCL)`で`state.<generation>.json`へ確定、transaction directory `fsync`する。

mutableな`current`は使わない。各generationのpointerを`current.<generation>.tmp`へ`O_EXCL|O_NOFOLLOW,0600`でwrite/fsyncし、`RENAME_EXCL`でimmutableな`current.<generation>`へ確定してdirectoryを`fsync`する。rename直後にpointerを`O_RDONLY|O_NOFOLLOW`でreadbackし、`fstat`のdev/ino/uid/modeと内容のtxn ID/generation/state checksumが作成時記録と一致しなければ停止する。回復時は全pointer/stateを検証し、連続した最大generationだけを採用する。

state確定後・pointer確定前のcrashは正常なwrite-ahead窓とする。generation `N-1`までstate/pointerが1対1でvalid、`state.N.json`だけがvalid checksum・同txn ID・generation N、`current.N`と`current.N.tmp`が欠落、Nより大きいentryがない場合は、state Nのphaseとfilesystem identityを検証して`current.N.tmp`を新規作成し、通常手順で`current.N`へ確定・readbackする。validな`current.N.tmp`だけが残る場合も内容をstate Nへ照合して確定し、不一致tempは削除せず隔離する。stateなしpointer、generation gap、複数orphan stateは破損として停止する。

`PREPARED`確定後にstaged appとdestinationを`RENAME_SWAP`し、Applications directoryを`fsync`、次generationの`SWAPPED`を確定する。新destination検証後に`VERIFIED`を確定し、stagingへ移った旧appだけをfd相対削除してtransaction directoryを`fsync`する。検証またはfsync失敗時は両名のidentity/hashがswap後配置である場合だけ再swapし、Applicationsを`fsync`、旧版を再検証する。

新規installも`PREPARED_NEW` stateを先にdurable確定し、staged appをApplications dirfd相対`RENAME_EXCL`でdestinationへrenameする。rename後Applicationsを`fsync`し、destination検証後に`VERIFIED`を確定する。crash recoveryはdestination=new・staged欠落ならforward、destination欠落・staged=newなら未公開としてcleanupし、それ以外は停止する。検証失敗時はnew identity/hashが一致するdestinationだけをtransactionへ`RENAME_EXCL`で戻し、Applicationsを`fsync`する。

起動時はowner/mode/nameが正しい全`state.<generation>.json`と`current.<generation>`を走査し、schema/checksum/txn ID/generation連続性を検証する。上記の単一highest orphan state/pointer-tempだけは補完可能な正常crashとして扱う。それ以外のchecksum不正、重複・欠落generation、未知schema、別txn IDなら何もrename/unlinkしない。identity/hash不整合でも「transactionを隔離して再実行」の固定案内で停止する。`PREPARED`、`SWAPPED`、`VERIFIED`の回復判定はconfig/backup/tempとdestination/stagedのold/new identity/hashをすべて照合してforward/rollbackを選ぶ。

- [ ] **Step 6: app/config transactionを配置結果照合によるforward recoveryへ固定する**

`install_with_config()`はconfigを書き換える前にTask 5 lockとtransaction directoryを作る。Task 4は新config tempをまず`O_EXCL|O_NOFOLLOW,0600`でwrite/fsyncするが、まだrenameしない。そのtempのbasename・`dev/ino/uid/mode/size/hash`、既存configのold hashとidentity（なければ`old_config=None`）、予測不能backup basename、rollback時にnew configを一時退避する予測不能trash basename、backupの期待identity（既存configと同じ）、old/new app identity/hashを確定する。これらと128-bit `txn_id`、`generation=1`、phase `TXN_PREPARED`を共通state/pointerへdurable確定してからだけconfig renameを開始する。Task 4の`ConfigTransaction`は指定temp/backup/trash basenameとtxn IDを使い、独自名やtxn IDを作らない。

app状態はmarker照合後の3値だけを使う。`absent`はmarkerの`old_app=None`かつdestination/stagedとも欠落する場合だけvalid、`old`はold app identity/hash一致、`new`はnew app identity/hash一致とする。markerがold appを記録しているのにdestinationが欠落する等は表へ入れずidentity不一致として隔離する。各操作前にconfig/backup/temp/appのdev/ino/uid/mode/size/hashを再照合し、rename/unlinkごとにdirectory `fsync`して同じ行を再評価可能にする。

| Config state | App absent | App old | App new |
|---|---|---|---|
| (a) existing: `config=old, backup=absent, temp=new` | tempを削除し`(no app, old config)` | tempを削除し`(old app, old config)` | old configをbackupへ`RENAME_EXCL`、tempをconfigへ`RENAME_EXCL`、backupを削除し`(new app, new config)` |
| (b) existing: `config=absent, backup=old, temp=new` | backupをconfigへ戻し、tempを削除して`(no app, old config)` | backupをconfigへ戻し、tempを削除して`(old app, old config)` | tempをconfigへ公開し、backupを削除して`(new app, new config)` |
| (c) existing: `config=new, backup=old, temp=absent` | new configを記録済みtrashへ退避、backupをconfigへ戻し、trashを削除して`(no app, old config)` | 同じrollbackで`(old app, old config)` | backupを削除して`(new app, new config)` |
| (d) initial: `config=absent, backup=absent, temp=new` | tempを削除し`(no app, no config)` | tempを削除し`(old app, no config)` | tempをconfigへ`RENAME_EXCL`で公開し`(new app, new config)` |
| (e) initial: `config=new, backup=absent, temp=absent` | new configをtrashへ退避後に削除し`(no app, no config)` | new configをtrashへ退避後に削除し`(old app, no config)` | 変更せず`(new app, new config)` |

表にないconfig/backup/temp配置、同一名の余分なentry、app identity不一致、必要な`RENAME_EXCL` collisionは収束不能とする。回復開始時の全対象bytesとinode一覧を保存し、何もrename/unlinkせず隔離する。特に(d)+app newはrollbackせずtemp newをconfigへ公開する。

回復mutation自体にもwrite-ahead stateを使う。各操作前に次generationへ`phase=RECOVER_<ACTION>`、対象のexact pre snapshotとpost snapshot（各nameのabsentまたはdev/ino/uid/mode/size/hash）をdurable確定する。操作とdirectory `fsync`後に次phaseを確定する。crash後にcurrent phaseのpre snapshotなら操作を実行し、post snapshotなら操作済みとして次へ進む。pre/postどちらでもなければ何も変更せず隔離する。

| Recovery action | Exact pre filesystem state | Exact post filesystem state |
|---|---|---|
| `OLD_TO_BACKUP` | `config=old, backup=absent, temp=new, trash=absent` | `config=absent, backup=old, temp=new, trash=absent` |
| `PUBLISH_TEMP` | `config=absent, backup=X, temp=new, trash=absent` | `config=new, backup=X, temp=absent, trash=absent`。`X`はmarkerどおりoldまたはabsent |
| `NEW_TO_TRASH` | `config=new, backup=X, temp=absent, trash=absent` | `config=absent, backup=X, temp=absent, trash=new` |
| `RESTORE_BACKUP` | `config=absent, backup=old, temp=X, trash=Y` | `config=old, backup=absent, temp=X, trash=Y`。`X`はnew/absent、`Y`はnew/absent |
| `DROP_TEMP` | markerどおりの他name、`temp=new` | 同じ他name、`temp=absent` |
| `DROP_BACKUP` | markerどおりの他name、`backup=old` | 同じ他name、`backup=absent` |
| `DROP_TRASH` | markerどおりの他name、`trash=new` | 同じ他name、`trash=absent` |

基底セルごとのaction列は固定する。(a)+absent/old=`DROP_TEMP`、(a)+new=`OLD_TO_BACKUP → PUBLISH_TEMP → DROP_BACKUP`、(b)+absent/old=`RESTORE_BACKUP → DROP_TEMP`、(b)+new=`PUBLISH_TEMP → DROP_BACKUP`、(c)+absent/old=`NEW_TO_TRASH → RESTORE_BACKUP → DROP_TRASH`、(c)+new=`DROP_BACKUP`、(d)+absent/old=`DROP_TEMP`、(d)+new=`PUBLISH_TEMP`、(e)+absent/old=`NEW_TO_TRASH → DROP_TRASH`、(e)+new=操作なしとする。このaction表が許す中間形は基底(a)–(e)外でも正常回復し、それ以外だけを隔離する。

固定順序は、(1)bundle build/manifestとconfig temp fsync、(2)共通`TXN_PREPARED` durable確定、(3)config rename、(4)同じtxn IDのapp `PREPARED`確定とapp配置、(5)new app/config hash再検証、(6)`COMMITTING` generation確定、(7)config backup削除とdirectory `fsync`、(8)`CLEANED`確定、(9)再開可能cleanupとする。config backup削除だけが失敗した場合はappを旧版へ戻さず、`COMMITTING` stateから次回forward cleanupする。

`CLEANED`後にtransaction directory自身のstateを先に消さない。installer parent dirへ`cleanup.<txn_id>.json`を`0600`でdurable確定し、transaction directoryのdev/inoと、削除対象の相対名・dev/ino/type/size/hashをexact listで記録する。`CLEANED` pointer確定後・receipt確定前にcrashした場合は、CLEANED stateと全children identityから同じreceiptを再生成する。各entryは存在すればidentity一致時だけfd相対削除、既に欠落なら完了済みとして扱い、1 entryごとにtransaction directoryを`fsync`する。全children欠落後、記録したdev/inoのtransaction directoryだけを`rmdir`してparentを`fsync`し、最後にcleanup receiptをunlinkしてparentを再`fsync`する。どの削除点でcrashしてもreceiptから再開し、未知entryまたはidentity差替えでは削除せず停止する。

testsは上記各操作の直前・直後へfault injectionし、各caseを新processで`recover_transactions()`して、公開状態が必ず(old app, old config)または(new app, new config)のどちらかであること、mixed pairを残さないこと、txn ID不一致・backup inode差替え・hash不一致では全bytes/inodeが不変で停止することをassertする。

```python
NORMAL_CRASH_POINTS = (
    "after_state_rename_before_pointer", "after_pointer_rename_before_readback",
    "before_config_backup_rename", "after_config_backup_rename",
    "before_config_publish_rename", "after_config_publish_rename",
    "before_app_prepared_state", "after_app_prepared_state",
    "before_app_swap", "after_app_swap", "after_verified_state",
    "after_committing_state", "after_cleaned_state",
    "after_cleanup_receipt", "after_each_cleanup_unlink", "after_transaction_rmdir",
    "after_uninstall_trash_rename", "after_uninstall_cleaned",
    "after_each_uninstall_cleanup_unlink",
)

def test_every_normal_crash_recovers_in_a_new_process_without_quarantine(self):
    for point in NORMAL_CRASH_POINTS:
        with self.subTest(point=point):
            fixture = create_isolated_transaction_fixture()
            fixture.crash_worker_process(point)
            result = fixture.run_recovery_process()
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("隔離", result.stderr)
            self.assertIn(fixture.public_hash_pair(), {
                fixture.old_hash_pair, fixture.new_hash_pair,
            })
            fixture.assert_no_mixed_app_config_pair()
```

- [ ] **Step 7: 回復可能な一般ユーザーuninstallとconfig削除を実装する**

`uninstall_app.py`はHOME/Applications/appをdirfd相対に開き、現在UID owner、同一device、固定bundle ID、exact allowlist、非symlinkを再検証する。install transaction lockと同じ`flock`を取得し、別install/updateとの並行実行を拒否する。transaction内へ`UNINSTALL_PREPARED` stateをdurable確定後、appを128-bit random `trash.<id>.app`へ`RENAME_EXCL`し、Applicationsを`fsync`、`UNINSTALL_MOVED`を確定してからexact allowlistの既知inodeだけをfd相対削除し、`UNINSTALL_CLEANED`を確定する。

crash recoveryはdestination=old・trash欠落ならrename前、destination欠落・trash=oldなら削除をforward completionする。destinationとtrashの両方がある、identity/hash不一致、state checksum/generation破損では何も削除せず停止する。`UNINSTALL_CLEANED`後も同じparent cleanup receiptへtrash、state、pointer、transaction directoryのidentityを記録して段階削除を再開可能にし、receiptを最後に消す。uninstall transactionはinstall/updateと同じstate schema/checksum/atomic pointer/cleanup実装を共有するが、install operationを呼ばない。

`remove_config=True`はapp削除成功後にTask 4と同じHOMEからのdirfd chain、owner/mode検査を使い`config.json`だけをunlinkしてdirectory `fsync`する。`remove_keychain=True`は引き続き常に拒否し、キーチェーン削除API・shell commandを実装しない。

- [ ] **Step 8: focused test、macOS syscall contract test、全回帰を通す**

Run: `python3 -m unittest tests.python.test_macos_installer tests.python.test_macos_uninstaller -v`

Run on macOS: `python3 -m unittest tests.python.test_macos_installer.RenameAtxContractTests -v`

Run: `bash tests/run-all.sh`

Run: `git diff --check`

Expected: 非昇格配置、manifest identity差替え、exact allowlist fd-copy、mount/owner/temp collision拒否、`RENAME_SWAP`更新、state atomic replace/checksum/generation/corrupt recovery、新規`RENAME_EXCL`、app/config forward recovery、uninstall trash recovery、既定設定・キーチェーン保持がPASS。全suiteとpublic secret scanもPASSし、`git diff --check`は出力なし。

- [ ] **Step 9: コミットする**

```bash
git add macos/install_transaction.py macos/uninstall_app.py macos/install_app.py tests/python/test_macos_installer.py tests/python/test_macos_uninstaller.py
git commit -m "feat: safely install and remove macOS app"
```

---

### Task 6: 安定private entrypoint、remote validator、atomic release state

**Files:**
- Create: `bin/stable-mail-entrypoint.php`
- Create: `bin/validate-release.php`
- Create: `src/ReleaseValidator.php`
- Create: `manager/release_deployer.py`
- Create: `manager/remote_validator.py`
- Modify: `manager/ftps_deployer.py`
- Modify: `manager/manage.py`
- Modify: `manager/xserver_api.py`
- Modify: `manager/keychain.py`
- Modify: `macos/install_transaction.py`
- Modify: `macos/install_app.py`
- Modify: `macos/local_config.py`
- Test: `tests/php/test_stable_bootstrap.php`
- Test: `tests/php/test_release_validator.php`
- Test: `tests/python/test_release_deployer.py`
- Create: `tests/python/test_remote_validator.py`
- Modify: `tests/python/test_macos_local_config.py`
- Modify: `tests/python/test_manager.py`
- Modify: `tests/python/test_macos_install_transaction.py`
- Modify: `tests/run-all.sh`

**Interfaces:**
- Consumes: `FtpsDeployer.deploy_release(local_dir, remote_root)`、`XServerApi.add_filter/list_filters/delete_filter`、既存固定private config。秘密config本文をlocalへ保存・releaseへ複製しない。
- Produces: standalone bootstrap、`RemoteValidator(ssh_alias, runner).validate(...) -> ValidationResult`、`ReleaseDeployer.bootstrap_migrate(...)`、`stage_and_validate(...)`、`switch_locator(...)`。FTPSは転送/readback、SSHはremote CLI/metadata validation、APIは初回本番filter移行だけ。

- [ ] **Step 1: Task 5の完了・clean gateを確認する**

Task 5実装commitがHEADの祖先で、fresh reviewerのCritical/Important/Minorが0、worktreeがcleanでなければ開始しない。`macos/install_transaction.py`等の未追跡物が1件でもあればTask 5へ戻し、Task 6 commitへ混ぜない。

- [ ] **Step 2: standalone bootstrapと単一locator schemaのREDテストを書く**

`tests/php/test_stable_bootstrap.php`はbootstrapをrelease、`vendor`、autoloadなしの一時固定private treeへ単独copyして実行する。locatorの唯一のschemaを`schema_version/release_id/release_path/entrypoint/manifest_sha256/config_path`へ固定し、unknown/missing key、relative/dot segment/大小文字`public_html`、locator・親・release componentのsymlink、directory `0755`、file `0640`、owner差、lstat/open間dev/inode差替え、64 KiB超過を追加する。`config_path`は初回の既存絶対pathから通常更新で変更不可、release内fallbackなしとする。

同じRED cycleでローカル設定を6-key schemaへ拡張する。`ssh_alias`は非空ASCIIのalias tokenだけを許可し、空白、先頭`-`、`@`、slash、colon、shell metacharacterを拒否する。既存5-key設定はinstallerでaliasを一度尋ねて明示確認後だけatomic migrationし、他5値をbyte一致で保持する。接続先、ユーザー、ポート、鍵pathは尋ねず保存しない。

stable entrypointはlocatorのabsolute releaseを解決し、固定private configを`MAIL_NOTIFIER_CONFIG`へ設定してversioned absolute CLIを呼ぶ。次をprocess testで固定する。

```php
$result = runStandaloneBootstrap($stable, $locator, "From: test@example.invalid\n\nbody");
assert($result->exitCode === 0);
assert($result->argv === ['/usr/bin/php8.5', '/home/account/private/releases/release-abc/bin/mail-to-lineworks.php']);
assert($result->env['MAIL_NOTIFIER_CONFIG'] === '/home/account/private/config.json');
assert(!str_contains($result->stdout . $result->stderr, 'webhook.worksmobile.com'));
```

- [ ] **Step 3: immutable bootstrap初回配置とlocator atomic updateをGREENにする**

Run: `php tests/php/test_stable_bootstrap.php`

Expected before: standalone bootstrap不足でFAIL。実装後: PASS。bootstrap自身が単一locator schema、各componentの`lstat`、owner、exact mode、open後dev/inode、fixed config pathを検査し、外部classをrequireしない。初回配置後のbootstrap hash/inodeは全release update/rollbackで不変とする。remote locator更新は同一directoryの完全tempを単一renameし、downloadしたbytes/hash/schema/modeをreadbackする。temp upload、rename前後、readback失敗の各fault pointへ実メール起動を注入し、old/newどちらか一回・mixed観測0を検査する。remote `fsync`成功のmock/assertは作らない。server crash後はreadbackしたvalid old/newへrollback/forwardし、partial/未知bytesではfilterを変えず停止する。

remote layout定数は全Python/PHP/testで次の一表から生成し、文字列を重複定義しない。

| key | absolute layout |
|---|---|
| `BOOTSTRAP` | `<home>/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php` |
| `LOCATOR` | `<home>/private/xserver-mail-lineworks/state/active-release.json` |
| `CONFIG` | 既存`config_path`（非移動・非複製） |
| `RELEASES` | `<home>/private/xserver-mail-lineworks/releases` |
| `TXNS` | `<home>/private/xserver-mail-lineworks/deploy-transactions` |

- [ ] **Step 4: SSH remote validatorのREDテストを書く**

canonical manifestをrelative pathのUTF-8 byte昇順・key順・compact JSONとして生成する。directory `0700`、通常file `0600`、実行対象PHP `0700`を列挙する。exact argvはdestination前にoption terminatorを置く`[/usr/bin/ssh, fixed -o options..., --, validated_alias, compile_time_remote_command]`とする。fixed optionsは`BatchMode=yes`、`StrictHostKeyChecking=yes`、`UserKnownHostsFile=%d/.ssh/known_hosts`、`GlobalKnownHostsFile=/dev/null`、`UpdateHostKeys=no`、`PermitLocalCommand=no`、`RemoteCommand=none`、`ProxyCommand=none`、`ProxyJump=none`、`KnownHostsCommand=none`、`CanonicalizeHostname=no`、`ClearAllForwardings=yes`、`ForwardAgent=no`、`ControlMaster=no`、`ControlPath=none`、`RequestTTY=no`、`LogLevel=ERROR`。`HostKeyAlias`はargvへ渡さない。aliasをoption、remote command文字列、stdinへ連結せずdestinationの1 argvだけにする。manifest/path等のdataは上限付きstdin JSONだけで渡す。

`tests/python/test_remote_validator.py`は実`/usr/bin/ssh -G`へ同じproduction argv contract（`-G`だけを検査modeとして追加）を渡し、option terminator/destination/単一remote commandがOpenSSHに受理されることをnetwork接続なしで確認する。fake runnerでも全argvをexact比較し、alias/dataがremote commandへ現れないことをassertする。

SSH trust preflightは`~/.ssh` current UID/exact `0700`、main `config` current UID/non-symlink/`0600`、固定`known_hosts` current UID/non-symlink/`0600`を要求する。`ssh -G`前のraw parserは64 KiB上限、UTF-8/NUL検査、odd unescaped backslash継続行結合、quote/escape状態、unquoted/unescaped `#` comment境界を処理し、logical lineの最初のASCII tokenをcase-foldする。directiveが`include`、`match`、`hostkeyalias`ならglobal/Host内を問わず拒否し、曖昧tokenizationも拒否する。selected aliasはwildcard/negation/複数patternでなくexact単独Host stanzaでなければ拒否する。

実行と同じoverrideの`ssh -G -- alias`をbounded stdoutでparseし、resolved hostnameが`ftps_host`または`servername`と一致、effective `hostkeyalias`が未設定/default、dangerous command/proxy/forward/canonicalize/control fieldsが無効、known-hosts pathが固定fileであることを検査する。expanded user/hostname/identityfile/stdout/stderrとconfig/known_hosts path/contentを表示・snapshot・logしない。raw parse/`ssh -G`後とactual SSH spawn直前にmain config/known_hostsのdev/inode/uid/mode/size/hashを再照合する。

```python
def test_ssh_validation_requires_manifest_mode_and_cli_dry_run(self):
    result = self.validator.validate(self.local_manifest)
    self.assertEqual("PASS", result.manifest)
    self.assertEqual("PASS", result.php_cli)
    self.assertEqual("PASS", result.absolute_cli_dry_run)
    self.assertFalse(result.symlinks)
```

missing/extra file、hash/size/mode差、symlink、SSH nonzero/timeout/unknown-or-changed-host-key、config/known_hosts symlink/loose mode/wrong owner、wildcard alias、effective hostname mismatch、`hostkeyalias`設定あり、`ssh -G` parse/oversize failure、dangerous option非none、malformed validator JSON、CLI非実行、dry-run秘密出力をFAILにする。raw parser negative testは`Include`/`Match`/`HostKeyAlias`の大小文字、global、全Host、indent、quote、escaped comment、通常comment、odd-backslash継続、`Match exec`、NUL、invalid UTF-8、unclosed quote/escapeを含める。実行直前identity差替えで停止し、検査済みfdをOpenSSHへ渡せないTOCTOU限界もREADME assertionへ加える。

実Mac contract testはproduction known_hostsを変更せずowner-only temp copyを2つ作る。resolved Xserver hostnameのentryを欠落させたcopyと、同じkey typeの有効な別test public keyへ置換したcopyをtest-only `UserKnownHostsFile` injectionでactual `/usr/bin/ssh`へ渡し、StrictHostKeyCheckingにより各々unknown/changed固定分類で非0終了することを確認する。正常copyは接続成功する。expanded hostname、known_hosts line、SSH stderrは捕捉後破棄し、test/reportへ表示しない。

- [ ] **Step 5: validatorを実装しfocused testをGREENにする**

`validate-release.php`はSSHのremote PHP CLIとしてmanifest JSONをstdinから上限付きで読み、releaseを非追従walkしてmanifest/mode/symlink、PHP version/extensions、autoload、absolute CLI dry-runを検証する。stdoutは完全schemaの非秘密結果JSONだけ、stderrは固定分類だけとする。`--audit-public-root`はserver homeと`public_html`のsymlink/owner/mode metadata、既知製品basenameの一致件数だけを返し、未知fileをopen/hashせず名称も返さない。

Run: `php tests/php/test_release_validator.php && python3 -m unittest tests.python.test_remote_validator tests.python.test_release_deployer -v`

Expected: PASS。

- [ ] **Step 6: FTPS upload/readbackとSSH validationを束ねる**

`ReleaseDeployer.stage_and_validate()`はFTPS upload/download hash readback後に`RemoteValidator.validate()`を呼ぶ。SSH結果でexact mode、symlink 0、PHP CLI、autoload、dry-run、private manifest、最小`public_html` metadata監査が全PASSになるまでlocatorを変更しない。FTPS `SITE CHMOD`応答だけでは成功にしない。SSH validatorはremote `fsync`を主張せず、same-directory rename/readback/recovery制約を維持する。

testはFTPS upload/readback mismatch、SSH接続/validator/schema/mode/public audit failureを個別注入し、すべてlocator/filter不変をassertする。validation中のXserver API `add_filter/delete_filter` call countは0で、一時validator filter、合成mail、nonce receiptを作らないことをnegative assertionにする。

- [ ] **Step 7: 初回filter集合移行と通常locator切替えを分離する**

`bootstrap_migrate()`だけが全managed filterを変更する。開始時に全recipient/domain/ruleを公式API readbackし、rule IDとcanonical非秘密representationを集合snapshotへ保存する。最初に公式rule evaluation semanticsと実環境probeで同一mailの複数matching ruleが一回だけaction実行されるかpreflightする。保証できる場合だけoverlap方式を使う。

保証できない場合は利用者に最大停止時間、LINE WORKS通知が停止し得ること、copy原本はmailboxへ残ることを日本語表示して明示確認し、maintenance markerをupload/download readbackする。全old削除/0件readback時刻をwindow開始、全new readback時刻をwindow終了として記録し、old/newを同時存在させない。mailbox認証や取得経路を追加しないため自動再投入は行わず、window中は従業員がmailbox原本を確認する。完了時に「二重LINE WORKS通知0、原本消失0、window中の通知停止は許容」を秘密なしで表示する。

| locator/bootstrap | filter集合 | 許可される収束 |
|---|---|---|
| old pair | 全old | semantics保証時だけ全new追加→集合readback→new pair確定→全old削除/readback |
| new pair | 全old+全new | 全old削除してnew/newへforward |
| old pair | 全old+全new | 全new削除してold/oldへrollback |
| new pair | 全new | commit/cleanup |
| その他 | 任意 | bytes/filterを変更せず隔離 |

0/1/複数recipient、複数domain、同条件重複、途中add/delete、readback reorder、未知rule出現をtestする。各mutation後に集合完全readbackし、process-kill faultを各rule追加/削除前後へ置く。remote testはvisibility/readback/recoveryだけを契約とし、file/directory `fsync`成功をmockしない。maintenance fallbackはwindow中に複数mailを注入し、LINE WORKS二重通知0、mailbox原本消失0、window外のold/new各smokeが一回だけ通知されることをE2Eする。初回移行commit後の`switch_locator()`はAPI mutationを呼ばず、old/new locatorのsame-directory rename/download readbackだけを行うことをnegative assertionで固定する。

- [ ] **Step 8: exact permission/public_html/secret non-duplication gateを追加する**

local manifestとSSH validator結果は全directory `0700`、通常file/locator/config/log `0600`、bootstrap/実行PHP `0700`をexact比較し、private treeのsymlinkは0を要求する。releaseにconfig、`.env`、Webhook URL、メールアドレス、API/FTPS credentialがないことを分類scannerで検査する。`public_html`はSSH最小audit結果の既知製品basename一致0件とroot/component非symlinkだけを要求し、未知内容を読まない。bootstrap、locator、releaseはFTPS download hash readbackとSSH metadata validationの双方を通す。

同じRED/GREEN cycleでinstaller completion receiptを追加する。receiptはtransaction ID、COMMITTED/CLEANED、最終app tree SHA-256、config SHA-256またはabsent marker、完了時刻、固定結果分類だけの完全schemaとし、秘密・設定値・pathを含めない。transaction cleanup前にinstaller parentの`completed/` `0700`へ`0600` temp、fsync、rename、directory fsyncで確定する。`--verify-completion-receipt --latest`はsymlink/owner/mode/schema/hashを再検査し、process終了だけを成功にしない。receipt write前後のcrash、tamper、stale receipt、app/config hash差のtestを追加する。

- [ ] **Step 9: 全test・diff hygiene・fresh reviewを通してcommitする**

Run: `bash tests/run-all.sh && git diff --check`

Expected: PASS、出力なし。Task 6の実装commit後、fresh subagent reviewでCritical/Important/Minor 0を得るまで次のTask 7へ進まない。

```bash
git add bin/stable-mail-entrypoint.php bin/validate-release.php src/ReleaseValidator.php manager/release_deployer.py manager/remote_validator.py manager/ftps_deployer.py manager/manage.py manager/xserver_api.py manager/keychain.py macos/install_transaction.py macos/install_app.py macos/local_config.py tests/php/test_stable_bootstrap.php tests/php/test_release_validator.php tests/python/test_release_deployer.py tests/python/test_remote_validator.py tests/python/test_manager.py tests/python/test_macos_install_transaction.py tests/python/test_macos_local_config.py tests/run-all.sh
git commit -m "feat: add stable private release entrypoint"
```

---

### Task 7: README、統合検証、実Macインストール

**Files:**
- Modify: `README.md`
- Modify: `tests/run-all.sh`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Tasks 1–6のインストーラー、ランチャー、stable private entrypoint、remote validator。
- Consumes: 公開対象feature branchのcommit SHA、既存の非公開Xserver設定、既存Keychain項目。実値は変数へ読み込まず、README、shell履歴、test output、作業reportへ転載しない。
- Produces: 公開可能なインストール・更新・Gatekeeper・Keychain・アンインストール・添付メタデータ手順と、GitHubで公開済みのimmutable commit SHAから作ったversioned releaseの配備・E2E・rollback記録。

- [ ] **Step 1: Tasks 4–6のclean gateと必須file追跡を確認する**

Task 4、Task 5、Task 6の実装commitが現在branchの祖先で、各fresh reviewがCritical/Important/Minor 0、作業treeがcleanであることを、Task 7の編集前に確認する。失敗時はTask 7へ進まず、該当taskへ戻す。

```bash
test -z "$(git status --porcelain)"
git merge-base --is-ancestor 34d953a HEAD
git merge-base --is-ancestor 8828068 HEAD
git ls-files --error-unmatch \
  macos/install_app.command macos/install_app.py macos/install_transaction.py \
  macos/uninstall_app.py macos/launcher.sh \
  macos/local_config.py macos/runtime.py macos/AppLauncher.applescript \
  manager/manage.py manager/keychain.py manager/ftps_deployer.py manager/xserver_api.py \
  manager/release_deployer.py bin/stable-mail-entrypoint.php bin/validate-release.php \
  manager/remote_validator.py \
  src/ReleaseValidator.php \
  tests/python/test_macos_installer.py tests/python/test_macos_uninstaller.py \
  tests/python/test_macos_launcher.py tests/python/test_macos_local_config.py \
  tests/python/test_macos_runtime.py
```

Expected: 4 commandともexit 0。`git status --porcelain`は空で、全必須fileがGit追跡済み。SHAはこの計画のTask 4/5 docs preflight commitを指し、実装時にTask 4/5実装commitが別SHAなら、その実装commitにも同じ`merge-base --is-ancestor`検査を追加する。

- [ ] **Step 2: READMEの構造・安全性を強制する失敗検査を追加する**

`tests/run-all.sh`のpublic secret scanより前へ、`README.md`の見出し、操作command、保持規則、添付メタデータ仕様をexact substringで検査するPython blockを追加する。単語だけの弱い検査にせず、次の完全なassertionを使用する。

```python
readme = (root / "README.md").read_text(encoding="utf-8")
required = [
    "## Macアプリのインストールと更新",
    "Python 3.13または3.14",
    "`macos/install_app.command`",
    "`~/Applications/Xserverメール通知管理.app`",
    "6項目は秘密ではありませんが、実環境の値をREADME、issue、ログへ転載しないでください。",
    "Gatekeeperでブロックされた場合だけ",
    "システム設定」→「プライバシーとセキュリティ」",
    "隔離属性を削除するコマンドは使用しません。",
    "「常に許可」は選ばず、その起動に必要な読取りだけを許可します。",
    "拒否してもKeychain項目は変更・削除されません。",
    "再インストールはユーザー設定とKeychain項目を保持します。",
    "既定のアンインストールはアプリ本体だけを削除します。",
    "添付ファイル本体とインライン画像はLINE WORKSへ送信しません。",
    "ファイル名と復号後サイズだけを最大20件表示します。",
    "名称なし",
    "未知形式の秘密を完全には検出できません。",
    "FTPSではサーバー側fsyncを保証できません。",
    "SSH設定とknown_hostsは現在の利用者だけが書き込める必要があります。",
    "OpenSSHが設定pathを再度開くため、最終検査後の差替えを完全には防げません。",
    "SSH configのIncludeとMatchは安全のため使用できません。",
]
for phrase in required:
    if phrase not in readme:
        raise SystemExit("FAIL: README required assertion: " + phrase)
for forbidden in ("xattr -d", "xattr -c", "spctl --master-disable"):
    if forbidden in readme:
        raise SystemExit("FAIL: README must not bypass Gatekeeper: " + forbidden)
```

- [ ] **Step 3: README検査が意図した理由で失敗することを確認する**

Run: `bash tests/run-all.sh`

Expected: `FAIL: README required assertion: ## Macアプリのインストールと更新`で非0終了し、PHP/Pythonの環境不備や秘密scanではなくREADME不足が原因。

- [ ] **Step 4: READMEへ操作・添付メタデータ・安全境界を具体的に記載する**

`README.md`へ以下を実値なしの日本語で記載する。

- Finderでrepositoryを開き`macos/install_app.command`をダブルクリックすること、Python 3.13または3.14だけを許可し、未導入時はPython公式installerまたはHomebrewの公式手順を参照すること。installer全体を`sudo`で起動しないこと。
- installerが確認・保存する非秘密メタデータ6項目を、既存5項目と`ssh_alias`（Mac SSH configの接続alias）というキー名・用途だけで表にする。接続先、ユーザー、ポート、鍵path、API key、FTPS credential、Webhook URL、実メールは入力・保存・文書化しないこと、6項目も実環境値を文書・issue・logへ転載しないことを明記する。
- SSH config/key/Keychainの用意は利用者側で行い、アプリは`/usr/bin/ssh`へaliasだけを渡すこと、remote CLI/manifest/mode/symlink/`public_html` metadata検証はSSH、upload/download hash readbackはFTPS、本番filter移行/readbackだけはAPIという役割分担を明記する。
- `~/.ssh`、config、固定known_hostsはcurrent UID所有・非symlink・owner-onlyである必要があり、unknown/changed host key、wildcard alias、resolved hostname不一致、proxy/remote/local command設定では停止することを明記する。effective `ssh -G`のuser/hostname/identity pathは表示・logしない。
- main SSH configの`Include`、全`Match`、全`HostKeyAlias`は場所・大小文字・継続行を問わずfail closedで拒否し、exact単独`Host` aliasだけを許可することを明記する。actual argvへ`HostKeyAlias`を渡さず、resolved hostnameのknown_hosts entryだけをStrictHostKeyCheckingで照合する。
- config/known_hostsは実行直前にidentityを再検査するが、OpenSSHへ検査済みfdを渡せずpathを再openするため最終検査後の差替えを完全には防げない制約を明記する。
- 初回または更新時にKeychain確認が出た場合、要求元が`~/Applications/Xserverメール通知管理.app`の同梱管理CLIであり、対象が既存のAPI/FTPS internet-password項目であることを確認して、その起動に必要な読取りだけを許可する。「常に許可」は選ばない。拒否は項目を変更・削除せず、アプリを終了して再起動すれば再試行できる。アプリはKeychainをread-onlyで利用し、自動登録・更新・削除しない。
- Gatekeeper案内は「初回起動がブロックされた場合だけ、Finderでcontrol-clickして『開く』、またはシステム設定→プライバシーとセキュリティで当該アプリの『このまま開く』」とする。通常起動、未署名警告、破損/改変検出を別条件として記載し、破損/改変時は開かず再buildする。`xattr`やGatekeeper全体無効化を案内しない。
- 再installerはappだけを更新し設定とKeychainを保持する。既定uninstallはappだけ、設定削除は別の明示確認、Keychainはどちらでも保持し、Keychain削除はKeychain Accessで利用者が別途判断する。
- 添付は`Content-Disposition: attachment`のpartについて、制御文字除去・100 Unicode code point上限のfilename（欠落時`名称なし`）とtransfer decode後byte sizeだけを通知し、最大20件と超過件数を表示する。本体、inline画像、実行可能内容は送らず、メールボックスで確認する。既存の「添付とインライン画像は送信しません」は「本体を送信しない」に修正してメタデータ表示と矛盾させない。
- 公開scanの対象（Webhook token、API keyらしき形式、実メール、環境識別子、秘密file名、実`public_html` path、生成app、commit range、untracked file）と、未知形式を完全検出できない限界を明記する。
- Xserver remote更新はsame-directory renameのatomic visibilityとdownload/hash/schema/mode readback、old/new recoveryまでを保証し、FTPSではサーバー側file/directory `fsync`や電源断後durabilityを保証できないこと、これはXserver基盤依存であることを明記する。Macローカルinstallerの`fsync`保証と混同しない。

- [ ] **Step 5: README検査、全回帰、diff hygieneを通す**

Run: `bash tests/run-all.sh`

Expected: README exact assertions、PHP、Python、macOS unittest、syntax、tracked public secret scanがすべてPASS。

Run: `git diff --check`

Expected: 出力なし、exit 0。

- [ ] **Step 6: Task 7 docs/test変更をcommitし、feature branchを公開前検証する**

```bash
git add README.md tests/run-all.sh .gitignore
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: add macOS app operations"
```

Expected: staged pathは上記3件だけ。commit後`git status --porcelain`は空。

公開候補の全到達可能commitを固定し、各commit tree、各parentとの差分patch（削除内容を含む）、root commit patch、tracked/untracked/ignoredの実値、生成appをscanする。scannerは一致内容を表示せずcommit/path/分類だけを出す既存`tests/run-all.sh`を共通利用する。ignored列挙は`git ls-files --others --ignored --exclude-standard -z`をbyte順sortした決定的集合とし、nested Git repository、submodule、sparse checkout、skip-worktree fileを別検出する。symlink/socket/device/FIFO、権限で読めないfile、走査中dev/inode変更、64 MiB超はfail closedにし、無言skipしない。

```bash
BASE=$(git merge-base HEAD origin/main)
test -n "$BASE"
bash tests/run-all.sh --scan-range "$BASE..HEAD"
bash tests/run-all.sh --scan-reachable HEAD
bash tests/run-all.sh --scan-ignored-values
test -z "$(git ls-files --others --exclude-standard)"
test ! -e dist/Xserverメール通知管理.app
git diff --check "$BASE..HEAD"
```

Expected: HEADから到達可能な全commit tree・全parent patch・削除内容・tracked/untracked/ignored実値が秘密なし。生成appはまだ存在しない。local secretはrepository外へ移して再検査し、内容をreportへ貼らない。

- [ ] **Step 7: 実Macで新規installの非同期完了と成果物を検証する**

実Macの一般ユーザーsessionで開始する。既存app/configがある場合は、app tree hash、config SHA-256、Keychain itemの非秘密attributeだけをrepository外のmode `600`一時記録へ保存する。秘密値を取得する`security ... -w`は使わない。`open`は非同期なので、commandが返ったことをinstall完了と扱わない。installerはtransaction ID、最終app tree SHA-256、config SHA-256、完了時刻、結果分類だけのowner-only completion receiptを`~/Applications/.xserver-mail-lineworks-installer/completed/<txn>.json`へatomic確定する。

Run: `open macos/install_app.command`

Expected: TerminalのinstallerがPython 3.13/3.14を検出し、秘密を尋ねず非秘密6項目だけを確認する。Terminalに成功表示が出てinstaller processが終了するまで待ち、その後にのみ次へ進む。

```bash
while pgrep -f '[m]acos/install_app.command' >/dev/null; do sleep 1; done
test -d "$HOME/Applications/Xserverメール通知管理.app"
python3 macos/install_app.py --verify-completion-receipt --latest
```

Expected: process終了後にappが存在し、receiptがCOMMITTED/CLEANEDでapp/config hashのreadbackと一致する。timeoutは120秒とし、超過時はkillや再installをせず、Terminalの固定日本語error分類を記録して中断する。

Run: `open "$HOME/Applications/Xserverメール通知管理.app"`

Expected: Terminalが開き、日本語管理メニューを表示する。`0`で終了すると終了コード0の日本語表示後Enter待ちになる。

- [ ] **Step 8: Gatekeeper条件を分離して検証する**

- quarantine attributeがないlocal buildは通常の`open`成功だけを確認し、Gatekeeper警告が出るとはassertしない。
- GitHubからdownloadしてquarantineが付いた候補で「開発元未確認」ブロックが実際に出た場合だけ、READMEのcontrol-click「開く」またはシステム設定の「このまま開く」を検証する。許可後にbundle IDとtree hashを再検証する。
- 「アプリが壊れている」「改変されている」、bundle ID/hash不一致の場合はGatekeeper overrideを行わず中断し、公開SHAから再取得する。
- `xattr -d`、`xattr -c`、`spctl --master-disable`は実行しない。

- [ ] **Step 9: 一時Keychain itemで許可・拒否を検証し、本番item不変を確認する**

repository外に専用一時keychainを作り、その中へ専用service/account名とrandom passwordのtest itemを1件だけ登録する。ACLは`/usr/bin/false`だけへ限定して同梱appの読取りが必ずpromptになるよう構成する。global `security list-keychains`、`default-keychain`、login keychainのsearch/default/ACLは読取り確認だけで変更を禁止する。

`manager/keychain.py`の`Keychain` constructorへtest-only `keychain_path: str | None = None` DIを追加する。production `None`は従来どおりkeychain path引数なし、test時だけ検証済み絶対pathを`security find-*-password ... <temporary-keychain-path>`の末尾固定argvとして渡す。環境変数やlocal configからpathを読まず、bundleのproduction call siteは常に`None`。relative/symlink/wrong owner/mode、不正suffixを拒否し、argvとerror出力にpasswordを含めない。`tests/python/test_manager.py`でproduction argvにpathなし、test argvに一つだけexplicit path、全faultでglobal search/defaultと本番ACL不変を検査する。

1. 限定ACL itemの確認で「拒否」を選び、固定日本語Keychain errorだけを表示して安全終了することを確認する。
2. 同じ限定ACL itemで再起動し、要求元と一時attributeを確認して今回の読取りだけ「許可」する。「常に許可」は選ばない。許可後もitem ACLを再読出しし、永続trusted applicationが追加されていないことを確認する。
3. UI automationでprompt操作が再現不能なMacでは成功扱いにせずmanual gateの実施記録を要求する。mock ACL testとbundle hash検査は補助でありprompt検証の代替にしない。
4. production targetへ戻してmenu `9`の同期診断だけを実行する。追加・変更・削除・接続testは実行しない。
5. 正常終了、拒否、許可、例外、SIGINTの全caseでfinally cleanupをtestし、一時keychain/item/file残存0をreadbackする。global search/defaultは一度も変更せず、本番itemの非秘密attribute・件数・ACL・変更日時が完全不変、標準出力・Terminal scrollbackに秘密値がないことを確認する。

- [ ] **Step 10: install/update rollbackとcleanupを実Macで検証する**

既存正常app/configのhashをrepository外へ保存後、Task 5のtest用fault injectionを使ってswap後validation failureを1回発生させる。production bundleへfault flagを同梱せず、test harnessからdependency injectionする。

Expected: installerは失敗し、旧app tree hashと旧config SHA-256が完全一致で復元される。`~/Applications/.xserver-mail-lineworks-installer`に未完了transaction、staged app、temp stateが残らず、lock fileだけが現在UID `0600`で残る。config directoryにtemp/backup/forward-recovery markerが残らない。次の通常updateが成功し、新app hashへ切替わる一方、config SHA-256とKeychain metadataは不変。

続けて既定uninstall、config削除ありuninstallを別々に行う。既定ではappだけ消えconfig/Keychainは保持、config削除ありではappとconfigだけ消えKeychainは保持する。各case後にinstallerを再実行して次case用状態を復元し、最後は正常install済みに戻す。途中で失敗した場合は手動`rm -rf`せず、Task 5の検証済みtransaction recoveryを再実行する。

- [ ] **Step 11: install後bundle・権限・秘密・非追跡物を監査する**

Run: `plutil -lint "$HOME/Applications/Xserverメール通知管理.app/Contents/Info.plist"`

Expected: `OK`。

Run: `stat -f '%Sp %Su' "$HOME/Library/Application Support/XserverMailLineworks" "$HOME/Library/Application Support/XserverMailLineworks/config.json"`

Expected: directory `drwx------`、file `-rw-------`、現在ユーザー所有。

Run: `find "$HOME/Applications/Xserverメール通知管理.app" -type l -print -quit`

Expected: 出力なし。Task 4のexact allowlistとbundle ID `jp.example.xserver-mail-lineworks-manager`、全fileの現在UID owner/group-other non-writable、主要fileの実行bitを`validate_bundle()`のinstalled-bundle modeで再検査する。

Run: `bash tests/run-all.sh --scan-path "$HOME/Applications/Xserverメール通知管理.app"`

Expected: 生成app内に既知秘密形式、実メール、実環境識別子、実設定値なし。scannerは秘密本文を表示しない。

Run: `bash tests/run-all.sh`

Expected: インストール後も全検証PASS。

- [ ] **Step 12: feature branchをpushし、review済みPRをmergeしてremote SHAをread backする**

```bash
FEATURE=$(git branch --show-current)
test "$FEATURE" != main
git push --set-upstream origin "$FEATURE"
LOCAL_FEATURE_SHA=$(git rev-parse HEAD)
REMOTE_FEATURE_SHA=$(git ls-remote origin "refs/heads/$FEATURE" | cut -f1)
test "$LOCAL_FEATURE_SHA" = "$REMOTE_FEATURE_SHA"
```

Expected: feature branchだけがpushされ、remote feature SHAがlocal HEADと一致。`main`へ直接pushしない。GitHub上で全checkとreviewを完了し、merge方式はmerge commitへ固定する（squash/rebase禁止）。merge commitの第2親が`LOCAL_FEATURE_SHA`、第1親がmerge直前のremote mainであることをAPIとfetch後commit objectの双方で確認する。

merge後、localの古い`main`をsourceにせずremoteをread backする。

```bash
git fetch origin main
PUBLIC_SHA=$(git rev-parse refs/remotes/origin/main)
test -n "$PUBLIC_SHA"
git merge-base --is-ancestor "$LOCAL_FEATURE_SHA" "$PUBLIC_SHA"
test "$(git ls-remote origin refs/heads/main | cut -f1)" = "$PUBLIC_SHA"
git show --no-patch --format='%H %P' "$PUBLIC_SHA"
test "$(git rev-parse "$PUBLIC_SHA^2")" = "$LOCAL_FEATURE_SHA"
```

Expected: 公開`main`がfeature HEADを含み、`ls-remote` SHAとfetch済み`origin/main` SHAが一致。以後のbuild/deployは`PUBLIC_SHA`だけをsource of truthとする。

- [ ] **Step 13: GitHub公開後SHAからversioned releaseを作成しpost-install validateする**

GitHubの公開archiveを`PUBLIC_SHA`指定でrepository外の新規temp directoryへ取得し、archive展開後のsource treeを`git archive --format=tar "$PUBLIC_SHA"`の内容と比較する。release IDは`release-${PUBLIC_SHA}`、remote rootは既存private root配下の`releases/release-${PUBLIC_SHA}`とし、`current`へ上書き配備しない。`composer install --no-dev --prefer-dist --classmap-authoritative`後、`composer validate --strict --no-check-publish`、PHP lint、`bash tests/run-all.sh`、entrypoint/`vendor/autoload.php`存在、追跡allowlist、release tree秘密scanを実行する。

Expected: GitHubで公開されたSHAと同じsourceだけからreleaseが生成され、build cache、`.git`、tests、local config、log、`.env`、生成Mac appを含まない。検証失敗時はuploadしない。

Task 6の`ReleaseDeployer.stage_and_validate()`でversioned private pathへuploadする。FTPS download hash readbackと、認証済みSSH alias経由のrelative path/type/size/SHA-256/exact mode/symlink、PHP CLI/version/extensions、autoload、absolute CLI dry-runをlocal manifestと一致確認する。validation中の一時filter/API mutationは0回。stable entrypoint、locator、固定configはreleaseへ複製しない。全検証成功までactive locatorと本番filterを変更しない。

- [ ] **Step 14: versioned releaseへ切替え、E2E、失敗時rollbackを行う**

初回bootstrap migration済みで全managed filterがstable command targetのまま不変であることをAPI readbackする。切替え前に旧locator SHA/内容、管理対象filter集合のcanonical SHA、固定config SHA-256をrepository外mode `600`記録へ保存する。秘密config本文は保存・表示・更新しない。Task 6の`switch_locator()`でlocatorだけをatomic update/download readbackし、API add/deleteが0回、filter集合SHAが前後同一であることを確認する。

E2Eは次の順で行う。

1. Mac app menu `9`でAPI件数、固定config path、stable command path、locatorの`release-${PUBLIC_SHA}`が一致。
2. menu `10`のLINE WORKS接続testが成功し、秘密値がTerminal/logにない。
3. 通知対象test mailboxへ添付あり合成mailを1通送る。LINE WORKSには本文とsanitized filename/decoded size metadataだけが表示され、添付本体・inline画像はなく、mailboxに原本が残る。
4. menu `11`の期限付きerror mail testを行い、件名token一致の1通だけがerror経路を通り、期限後にtest設定が無効化されたことをreadbackする。

いずれかが失敗したら新versionを修正して上書きしない。locatorだけを旧SHAへatomic rollback/download readbackし、filter集合と固定configは変更しない。旧versionで同期診断と1通のsmokeを再実行し、rollback成功まで新releaseを削除しない。locator identityが表外またはrollback不能なら最後のreadback済みlocator/filter集合を変えず固定incident分類で中断する。

- [ ] **Step 15: `public_html`、log、release残骸を最終監査する**

SSH validatorの`--audit-public-root`で`public_html`を非追従metadataだけ監査し、既知製品basename一致0件を確認する。未知fileの内容/hash/nameを取得・記録しない。private rootではactive locator、旧rollback version、未知symlinkなしを確認する。全directoryはexact `0700`、通常file/locator/config/logはexact `0600`、bootstrap/実行PHPはexact `0700`であり、SSH metadataとFTPS hashをreadbackする。

JSON Lines logはowner-only mode、configで指定したprivate root内だけにあり、最新E2E eventの`outcome`/`classification`/`http_status`が期待値であることを確認する。本文、添付filename、email address、Webhook URL、API key、FTPS credential、例外本文がlogにないことを分類別scannerで検査し、一致文字列そのものは表示しない。監査結果には`PUBLIC_SHA`、release ID、時刻、PASS/FAIL分類、rollback実施有無だけを記録する。

最後にrepositoryで次を再実行する。

```bash
test -z "$(git status --porcelain)"
bash tests/run-all.sh --scan-range "$(git merge-base "$LOCAL_FEATURE_SHA" "$PUBLIC_SHA")..$PUBLIC_SHA"
bash tests/run-all.sh --scan-reachable "$PUBLIC_SHA"
bash tests/run-all.sh --scan-ignored-values
git diff --check "$(git merge-base "$LOCAL_FEATURE_SHA" "$PUBLIC_SHA")..$PUBLIC_SHA"
test "$(git ls-remote origin refs/heads/main | cut -f1)" = "$PUBLIC_SHA"
```

Expected: local worktree clean、公開commit rangeとremote main SHAが再確認済み、private versioned releaseのE2E成功、rollback可能な旧version保持、`public_html`とlogに秘密・配備残骸なし。
