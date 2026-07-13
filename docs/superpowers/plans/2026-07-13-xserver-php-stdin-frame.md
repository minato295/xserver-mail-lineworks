# Xserver PHP Standard Input Frame Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xserver PHP 8.5がFD 3を閉じる環境でも、検証済み秘密設定とメール原文を子PHPへ安全に渡し、恒久header filterからLINE WORKS通知を復旧する。

**Architecture:** stable bootstrapは検証済み設定と上限付きメール原文を固定binary frameとしてowner-onlyのunlinked一時ストリームへ書き、子のstdinへ渡す。子は固定flag時だけframeを厳格にdecodeし、設定を既存schemaで検証してから残りbytesをメール処理へ渡す。legacy CLIはflagなしの場合だけ維持する。

**Tech Stack:** PHP 8.5/8.1+ CLI、PHP標準ストリーム、既存Composer autoload、既存PHPテストランナー、Xserver Server API、SSH。

## Global Constraints

- 秘密設定をargv、環境変数、stdout、stderr、運用ログ、公開領域へ出さない。
- frame flagが完全一致した場合、形式不正をlegacy入力へフォールバックしない。
- 設定長は2〜65536 bytes、メールは最大10 MiB + 超過検出1 byteとする。
- CRLF、NUL、添付相当binaryをbyte-for-byte維持する。
- `public_html`へファイルを作らない。配備物は従来どおり700/600を維持する。
- 過去の欠落メールを自動再送しない。

---

### Task 1: Standard-input frame decoder

**Files:**
- Create: `src/StdinFrame.php`
- Create: `tests/php/test_stdin_frame.php`
- Modify: `tests/run-all.sh`

**Interfaces:**
- Consumes: readable PHP stream resource.
- Produces: `XserverMail\StdinFrame::decode($stream): array{configJson:string,message:string}`.
- Produces: `XserverMail\StdinFrame::MAGIC` and fixed config/message limits used by tests.

- [ ] **Step 1: Write the failing decoder tests**

Create a real stream with `php://temp` and assert valid config plus CRLF/NUL/binary message round-trips exactly. Add separate rejection cases for wrong magic, header/config EOF, high word nonzero, lengths 0/1/65537, scalar/list/invalid JSON (at the entrypoint integration boundary), and 10 MiB + 1 detection. The core valid fixture is:

```php
$config = '{"webhook_url":"https://webhook.worksmobile.com/message/test-placeholder"}';
$message = "From: test@example.invalid\r\n\r\nA\0B\xff";
$stream = fopen('php://temp', 'w+b');
fwrite($stream, XserverMail\StdinFrame::MAGIC . pack('NN', 0, strlen($config)) . $config . $message);
rewind($stream);
$decoded = XserverMail\StdinFrame::decode($stream);
frameCheck($decoded === ['configJson' => $config, 'message' => $message], 'frame bytes changed');
```

- [ ] **Step 2: Run the decoder test and verify RED**

Run: `php tests/php/test_stdin_frame.php`

Expected: FAIL because `XserverMail\StdinFrame` does not exist.

- [ ] **Step 3: Implement the minimal strict decoder**

Implement `readExact()` as a loop that rejects EOF/empty reads. Decode the length with `unpack('Nhigh/Nlow', ...)`, require `high === 0` and `2 <= low <= 65536`, then read the remaining message up to `10 * 1024 * 1024 + 1`. Do not normalize bytes.

```php
final class StdinFrame
{
    public const MAGIC = "XSERVER-MAIL-FRAME\0\x01";
    public const MAX_CONFIG_BYTES = 65536;
    public const MAX_MESSAGE_BYTES = 10485760;

    /** @return array{configJson:string,message:string} */
    public static function decode($stream): array
    {
        if (!is_resource($stream) || self::readExact($stream, strlen(self::MAGIC)) !== self::MAGIC) {
            throw new \InvalidArgumentException('Invalid input frame');
        }
        $length = unpack('Nhigh/Nlow', self::readExact($stream, 8));
        if (!is_array($length) || $length['high'] !== 0
            || $length['low'] < 2 || $length['low'] > self::MAX_CONFIG_BYTES) {
            throw new \InvalidArgumentException('Invalid input frame');
        }
        $configJson = self::readExact($stream, $length['low']);
        $message = stream_get_contents($stream, self::MAX_MESSAGE_BYTES + 1);
        if (!is_string($message) || strlen($message) > self::MAX_MESSAGE_BYTES) {
            throw new \InvalidArgumentException('Invalid input frame');
        }
        return compact('configJson', 'message');
    }

    private static function readExact($stream, int $length): string
    {
        $value = '';
        while (strlen($value) < $length) {
            $part = fread($stream, $length - strlen($value));
            if (!is_string($part) || $part === '') throw new \InvalidArgumentException('Invalid input frame');
            $value .= $part;
        }
        return $value;
    }
}
```

- [ ] **Step 4: Run the decoder test and full focused PHP tests**

Run: `php tests/php/test_stdin_frame.php && php tests/php/test_delivery.php`

Expected: both PASS with no warnings.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/StdinFrame.php tests/php/test_stdin_frame.php tests/run-all.sh
git commit -m "test: define strict notifier stdin frame"
```

### Task 2: Stable bootstrap frame writer

**Files:**
- Modify: `bin/stable-mail-entrypoint.php`
- Modify: `tests/php/test_stable_bootstrap.php`

**Interfaces:**
- Consumes: verified config bytes and Xserver maildrop stdin.
- Produces: child stdin `MAGIC + pack('NN', 0, config length) + config + raw mail`.
- Produces: environment `MAIL_NOTIFIER_STDIN_FRAME=1`; removes both legacy config environment names.

- [ ] **Step 1: Rewrite the bootstrap fixture expectations first**

Change the fixture entrypoint to parse the frame from stdin, record only non-secret assertions, and explicitly fail if `MAIL_NOTIFIER_CONFIG_FD` or `MAIL_NOTIFIER_CONFIG` exists. Assert that caller args are rejected except exactly one of `--check-config`/`--check-message`, and that the allowed value reaches the child. Add a fixture whose child process only has FD 0/1/2 to reproduce Xserver.

- [ ] **Step 2: Run and verify RED**

Run: `php tests/php/test_stable_bootstrap.php`

Expected: FAIL because the bootstrap still sets `MAIL_NOTIFIER_CONFIG_FD` and does not send a frame.

- [ ] **Step 3: Implement the standalone frame writer**

In the bootstrap, remove descriptor FD 3. Validate args with an allowlist. Create `tmpfile()`, require regular-file type, current UID, and mode 0600, then write with a loop:

```php
$frame = tmpfile();
if ($frame === false) bootstrapFail();
$frameStat = fstat($frame);
if ($frameStat === false || ($frameStat['mode'] & 0170000) !== 0100000
    || $frameStat['uid'] !== bootstrapUid() || ($frameStat['mode'] & 0777) !== 0600) bootstrapFail();
bootstrapWriteAll($frame, "XSERVER-MAIL-FRAME\0\x01" . pack('NN', 0, strlen($configBody)) . $configBody);
bootstrapCopyLimited(STDIN, $frame, 10485761);
if (fflush($frame) !== true || rewind($frame) !== true) bootstrapFail();
$descriptors = [0 => $frame, 1 => STDOUT, 2 => STDERR];
$environment['MAIL_NOTIFIER_STDIN_FRAME'] = '1';
unset($environment['MAIL_NOTIFIER_CONFIG_FD'], $environment['MAIL_NOTIFIER_CONFIG']);
```

Pass only allowlisted args in `[PHP_BINARY, $cursor, ...$childArgs]`. Keep verified runtime handles open through the final identity/hash pass and `proc_open` as before.

- [ ] **Step 4: Run bootstrap tests and verify GREEN**

Run: `php tests/php/test_stable_bootstrap.php`

Expected: PASS, including FD3-closed regression and secret non-exposure assertions.

- [ ] **Step 5: Commit Task 2**

```bash
git add bin/stable-mail-entrypoint.php tests/php/test_stable_bootstrap.php
git commit -m "fix: frame verified config over child stdin"
```

### Task 3: Entrypoint and release validation integration

**Files:**
- Modify: `bin/mail-to-lineworks.php`
- Modify: `bin/validate-release.php`
- Modify: `src/ReleaseValidator.php`
- Modify: `tests/php/test_delivery.php`
- Modify: `tests/php/test_release_validator.php`

**Interfaces:**
- Consumes: `StdinFrame::decode(STDIN)` when `MAIL_NOTIFIER_STDIN_FRAME === '1'`.
- Produces: `NotifierConfig::fromArray(json_decode(configJson, ...))` and exact raw message bytes.
- Preserves: flagなしの`MAIL_NOTIFIER_CONFIG` legacy CLI.

- [ ] **Step 1: Add failing entrypoint and validator tests**

Assert valid frame works when FD3 is absent; malformed flagged frame exits nonzero in check modes; flagなしメール beginning with the magic remains ordinary legacy input; legacy config path still works; secret config text never appears in output. Change release validator fixtures to require the frame flag and decode stdin instead of reading `/dev/fd/N`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `php tests/php/test_delivery.php && php tests/php/test_release_validator.php`

Expected: FAIL because the entrypoint and validators still use `MAIL_NOTIFIER_CONFIG_FD`.

- [ ] **Step 3: Integrate frame decoding in the entrypoint**

At startup, branch only on exact flag equality:

```php
$framed = getenv('MAIL_NOTIFIER_STDIN_FRAME') === '1';
if ($framed) {
    $frame = XserverMail\StdinFrame::decode(STDIN);
    $value = json_decode($frame['configJson'], true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($value) || array_is_list($value)) throw new InvalidArgumentException('Invalid configuration');
    $config = NotifierConfig::fromArray($value);
    $raw = $frame['message'];
} else {
    $config = NotifierConfig::load(getenv('MAIL_NOTIFIER_CONFIG') ?: NotifierConfig::defaultPath(__DIR__));
    $raw = readLegacyMessage(STDIN);
}
```

For `--check-config`, config construction must occur before exit. For `--check-message` and normal mode, parse/use `$raw`. Reject simultaneous check flags and unknown args.

- [ ] **Step 4: Convert both release dry-runs to the same frame protocol**

Replace all config FD descriptors/environment with an owner-only `tmpfile()` containing the same fixed frame plus validator message. Set only `MAIL_NOTIFIER_STDIN_FRAME=1`. Keep manifest revalidation, real-path Composer execution, output limits, and secret-output checks unchanged.

- [ ] **Step 5: Run focused and full local tests**

Run: `php tests/php/test_delivery.php && php tests/php/test_release_validator.php && bash tests/run-all.sh`

Expected: all PASS, no warnings, no secret values in output.

- [ ] **Step 6: Commit Task 3**

```bash
git add bin/mail-to-lineworks.php bin/validate-release.php src/ReleaseValidator.php tests/php/test_delivery.php tests/php/test_release_validator.php
git commit -m "fix: decode verified config from stdin frame"
```

### Task 4: Production verification and cleanup

**Files:**
- No source changes unless verification exposes a new failing test.

**Interfaces:**
- Consumes: existing Mac Keychain credentials, SSH host/key, Xserver permanent managed filters.
- Produces: new immutable release and one permanent-filter E2E result.

- [ ] **Step 1: Run pre-deploy verification**

Run: `bash tests/run-all.sh && git diff --check && git status --short`

Expected: tests PASS; only intended commits/changes.

- [ ] **Step 2: Deploy through the existing verified release workflow**

Use the Mac manager release command so manifest, ownership/modes, public-root audit, remote CLI dry-run, and atomic locator switch all run. Do not mutate permanent filters during release switching.

- [ ] **Step 3: Verify permanent filter E2E**

Send one new message with a unique Message-ID to the configured production mailbox, without adding a temporary subject rule. Verify mailbox arrival and exactly one new operational event with `classification=success` and `http_status=200` at the current timestamp. Public fixtures and documentation must use only `example.invalid` addresses.

- [ ] **Step 4: Audit cleanup and permanent state**

Read back Xserver API filters and confirm exactly the intended permanent managed rules remain. Remove all `/home/example/private/xserver-*-probe.php`, `/tmp/xserver-*-probe-*.json`, and local `/private/tmp/xserver-*probe*` diagnostic files after checking hashes/names. Confirm no probe rule remains.

- [ ] **Step 5: Push the completed main branch**

```bash
git push origin main
```

Expected: GitHub `main` contains the design, tests, and fix; no credentials, personal addresses beyond intentional configurable examples, `.eml`, logs, or diagnostic artifacts are tracked.
