<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';
require_once dirname(__DIR__, 2) . '/src/SendmailProcessAdapter.php';

use XserverMail\DeliveryHealthMonitor;
use XserverMail\NativePrivateStateFilesystem;
use XserverMail\OperationalLogger;
use XserverMail\PrivateStateFilesystem;
use XserverMail\SendmailClient;
use XserverMail\SendmailProcessAdapter;
use XserverMail\SendmailProcessHandle;
use XserverMail\SystemMailAuthenticator;

function healthCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

final class FakePrivateStateFilesystem implements PrivateStateFilesystem
{
    /** @var array<string,string> */
    public array $files = [];
    /** @var list<string> */
    public array $faults = [];
    public int $locks = 0;
    public int $reads = 0;
    public int $replaces = 0;

    public function withExclusiveLock(string $lockPath, callable $operation): mixed
    {
        ++$this->locks;
        if ($this->takeFault('lock')) {
            throw new RuntimeException('PRIVATE_FAULT_MARKER');
        }
        return $operation();
    }

    public function assertExclusiveLockCurrent(): void
    {
        if ($this->takeFault('lock_lease')) {
            throw new RuntimeException('PRIVATE_FAULT_MARKER');
        }
    }

    public function readRegular(string $path, int $limit): ?string
    {
        ++$this->reads;
        if ($this->takeFault('read') || $this->takeFault('wrong_owner')
            || $this->takeFault('replaced_inode') || $this->takeFault('readback')) {
            throw new RuntimeException('PRIVATE_FAULT_MARKER');
        }
        if (!array_key_exists($path, $this->files)) {
            return null;
        }
        if (strlen($this->files[$path]) > $limit) {
            throw new RuntimeException('PRIVATE_FAULT_MARKER');
        }
        return $this->files[$path];
    }

    public function replaceAtomic(string $path, string $bytes, int $mode): void
    {
        ++$this->replaces;
        foreach (['short_write', 'fsync', 'rename'] as $fault) {
            if ($this->takeFault($fault)) {
                throw new RuntimeException('PRIVATE_FAULT_MARKER');
            }
        }
        $this->files[$path] = $bytes;
        if ($this->takeFault('post_replace')) {
            unset($this->files[$path]);
            throw new RuntimeException('PRIVATE_FAULT_MARKER');
        }
    }

    private function takeFault(string $fault): bool
    {
        $index = array_search($fault, $this->faults, true);
        if ($index === false) {
            return false;
        }
        array_splice($this->faults, $index, 1);
        return true;
    }
}

final class HealthSendmailHandle implements SendmailProcessHandle
{
    public string $received = '';
    public bool $stdinClosed = false;

    public function __construct(private readonly int $exitCode)
    {
    }

    public function writeStdin(string $bytes): int
    {
        $this->received .= $bytes;
        return strlen($bytes);
    }

    public function closeStdin(): void { $this->stdinClosed = true; }
    public function readStdout(): string { return ''; }
    public function readStderr(): string { return ''; }
    public function status(): array
    {
        return $this->stdinClosed
            ? ['running' => false, 'exitCode' => $this->exitCode]
            : ['running' => true, 'exitCode' => null];
    }
    public function terminate(int $signal): void {}
    public function close(): int { return $this->exitCode; }
}

final class HealthSendmailAdapter implements SendmailProcessAdapter
{
    /** @var list<string> */
    public array $messages = [];
    public int $exitCode = 0;

    public function start(array $argv): SendmailProcessHandle
    {
        healthCheck($argv === ['/usr/sbin/sendmail', '-t', '-i'], 'Health mail must use fixed sendmail argv');
        return new class($this) implements SendmailProcessHandle {
            private string $bytes = '';
            private bool $closed = false;
            public function __construct(private readonly HealthSendmailAdapter $owner) {}
            public function writeStdin(string $bytes): int { $this->bytes .= $bytes; return strlen($bytes); }
            public function closeStdin(): void { $this->closed = true; $this->owner->messages[] = $this->bytes; }
            public function readStdout(): string { return ''; }
            public function readStderr(): string { return ''; }
            public function status(): array { return $this->closed
                ? ['running' => false, 'exitCode' => $this->owner->exitCode]
                : ['running' => true, 'exitCode' => null]; }
            public function terminate(int $signal): void { $this->closed = true; }
            public function close(): int { return $this->owner->exitCode; }
        };
    }
}

/** @return array{DeliveryHealthMonitor,FakePrivateStateFilesystem,HealthSendmailAdapter,string,string,SystemMailAuthenticator} */
function fakeMonitor(
    ?FakePrivateStateFilesystem $filesystem = null,
    ?HealthSendmailAdapter $adapter = null,
    ?callable $utcClock = null,
): array
{
    $filesystem ??= new FakePrivateStateFilesystem();
    $adapter ??= new HealthSendmailAdapter();
    $log = tempnam(sys_get_temp_dir(), 'health-log-');
    healthCheck(is_string($log), 'Health log fixture must exist');
    $path = '/private-test/delivery-health.json';
    $authenticator = new SystemMailAuthenticator(str_repeat('k', 32));
    $utcClock ??= static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00');
    $monitor = new DeliveryHealthMonitor(
        $path,
        ['operator@example.invalid'],
        '/private-test/operational.jsonl',
        $authenticator,
        new SendmailClient($adapter, static fn (): float => 0.0, static fn (): bool => true),
        new OperationalLogger($log, static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00')),
        $filesystem,
        $utcClock,
        static fn (): string => str_repeat('e', 32),
    );
    return [$monitor, $filesystem, $adapter, $path, $log, $authenticator];
}

/** @return array{headers:array<string,string>,subject:string,body:string} */
function decodeHealthWire(string $wire): array
{
    $boundary = strpos($wire, "\r\n\r\n");
    healthCheck($boundary !== false, 'Health wire must contain one header/body boundary');
    $headers = [];
    foreach (explode("\r\n", substr($wire, 0, $boundary)) as $line) {
        $separator = strpos($line, ': ');
        healthCheck($separator !== false, 'Health wire headers must use canonical syntax');
        $headers[strtolower(substr($line, 0, $separator))] = substr($line, $separator + 2);
    }
    $encodedSubject = $headers['subject'] ?? '';
    healthCheck(preg_match_all('/=\?UTF-8\?B\?([^?]+)\?=/D', $encodedSubject, $matches) > 0,
        'Health subject must use RFC 2047 UTF-8 Base64 encoded words');
    $subject = '';
    foreach ($matches[1] as $chunk) {
        $decodedChunk = base64_decode($chunk, true);
        healthCheck(is_string($decodedChunk), 'Health subject Base64 must decode strictly');
        $subject .= $decodedChunk;
    }
    $body = base64_decode(str_replace("\r\n", '', substr($wire, $boundary + 4)), true);
    healthCheck(is_string($body), 'Health body Base64 must decode strictly');
    return ['headers' => $headers, 'subject' => $subject, 'body' => $body];
}

[$monitor, $filesystem, $sendmail, $statePath, $logPath] = fakeMonitor();
healthCheck($monitor->status() === 'healthy', 'Missing health state must initialize healthy');
$initial = json_decode($filesystem->files[$statePath], true, 16, JSON_THROW_ON_ERROR);
healthCheck(array_keys($initial) === ['schema_version', 'status', 'changed_at', 'next_observation_sequence', 'last_applied_sequence'],
    'Healthy state must use the exact key set');
healthCheck($initial['next_observation_sequence'] === 0 && $initial['last_applied_sequence'] === 0,
    'Missing state must initialize both sequences to zero');
healthCheck($monitor->reserveObservation() === 1, 'First reservation must be sequence one');
$reserved = json_decode($filesystem->files[$statePath], true, 16, JSON_THROW_ON_ERROR);
healthCheck($reserved['next_observation_sequence'] === 1 && $reserved['last_applied_sequence'] === 0,
    'Reservation must advance only next_observation_sequence');

$reserved['next_observation_sequence'] = PHP_INT_MAX - 1;
$reserved['last_applied_sequence'] = PHP_INT_MAX - 1;
$filesystem->files[$statePath] = json_encode($reserved, JSON_THROW_ON_ERROR) . "\n";
healthCheck($monitor->reserveObservation() === PHP_INT_MAX, 'Last representable sequence must be reservable');
$maxBytes = $filesystem->files[$statePath];
healthCheck($monitor->reserveObservation() === null, 'Sequence exhaustion must fail open with no reservation');
healthCheck($filesystem->files[$statePath] === $maxBytes, 'Sequence exhaustion must leave state unchanged');
healthCheck(str_contains((string) file_get_contents($logPath), 'health_state_failure'),
    'Sequence exhaustion must emit only the safe health classification');
unlink($logPath);

[$reorderedMonitor, $reorderedFilesystem, , $reorderedPath, $reorderedLog] = fakeMonitor();
$reorderedFilesystem->files[$reorderedPath] = json_encode([
    'last_applied_sequence' => 0,
    'changed_at' => '2026-07-13T12:00:00Z',
    'schema_version' => 1,
    'next_observation_sequence' => 0,
    'status' => 'healthy',
], JSON_THROW_ON_ERROR) . "\n";
healthCheck($reorderedMonitor->reserveObservation() === 1,
    'Strict health schema must accept an exact object key set regardless of JSON member order');
unlink($reorderedLog);

foreach ([
    'duplicate' => '{"schema_version":1,"status":"healthy","status":"degraded","changed_at":"2026-07-13T12:00:00Z","next_observation_sequence":0,"last_applied_sequence":0}',
    'escaped_duplicate' => '{"schema_version":1,"status":"healthy","\\u0073tatus":"degraded","changed_at":"2026-07-13T12:00:00Z","next_observation_sequence":0,"last_applied_sequence":0}',
    'unknown' => '{"schema_version":1,"status":"healthy","changed_at":"2026-07-13T12:00:00Z","next_observation_sequence":0,"last_applied_sequence":0,"unknown":1}',
    'list' => '[]',
    'oversize' => str_repeat('x', 4097),
] as $name => $bytes) {
    [$invalidMonitor, $invalidFilesystem, , $invalidPath, $invalidLog] = fakeMonitor();
    $invalidFilesystem->files[$invalidPath] = $bytes;
    healthCheck($invalidMonitor->reserveObservation() === null, $name . ' state must fail closed for transitions');
    healthCheck($invalidFilesystem->files[$invalidPath] === $bytes, $name . ' state must not be overwritten');
    healthCheck(str_contains((string) file_get_contents($invalidLog), 'health_state_failure'),
        $name . ' state must produce a safe log event');
    unlink($invalidLog);
}

foreach (['wrong_owner', 'replaced_inode', 'short_write', 'fsync', 'rename', 'readback'] as $fault) {
    [$faultMonitor, $faultFilesystem, , , $faultLog] = fakeMonitor();
    $faultFilesystem->faults[] = $fault;
    healthCheck($faultMonitor->reserveObservation() === null, $fault . ' must fail open without a sequence');
    healthCheck(str_contains((string) file_get_contents($faultLog), 'health_state_failure'),
        $fault . ' must log the fixed safe classification');
    unlink($faultLog);
}

$realErrorBody = "LINE WORKSへのメール通知で障害が発生しました。\n"
    . "復旧するまで、LINE WORKSへ通知されない可能性があります。\n\n"
    . "【必要な対応】\n"
    . "Xserverのメールボックスで新着メールを直接確認してください。\n"
    . "このメールへの返信は不要です。\n\n"
    . "障害発生日時：2026年07月14日（火）17時33分55秒\n"
    . "障害内容：LINE WORKSに接続できませんでした。\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：transport_error\n"
    . "確認方法：Macの「Xserverメール通知管理」アプリで「同期診断」を実行してください。\n";
$realRecoveryBody = "LINE WORKSへのメール通知は復旧しました。\n"
    . "今後受信する対象メールは通常どおり通知されます。\n\n"
    . "障害中にLINE WORKSへ通知されなかったメールは自動では再通知されません。\n\n"
    . "【必要な対応】\n"
    . "障害発生日時から復旧日時までの新着メールを、\n"
    . "Xserverのメールボックスで確認してください。\n"
    . "このメールへの返信は不要です。\n\n"
    . "復旧日時：2026年07月14日（火）17時35分10秒\n"
    . "障害発生日時：2026年07月14日（火）17時33分55秒\n"
    . "障害内容：LINE WORKSに接続できませんでした。\n"
    . "現在の状態：正常\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：transport_error\n";
$orderedTimes = [
    new DateTimeImmutable('2026-07-13T12:00:00Z'),
    new DateTimeImmutable('2026-07-14T08:33:55Z'),
    new DateTimeImmutable('2026-07-14T08:35:10Z'),
];
$orderedClock = static function () use (&$orderedTimes): DateTimeImmutable {
    $time = array_shift($orderedTimes);
    healthCheck($time instanceof DateTimeImmutable, 'Ordered health clock must not be exhausted');
    return $time;
};
[$ordered, $orderedFs, $orderedMail, $orderedPath, $orderedLog, $orderedAuth] = fakeMonitor(
    utcClock: $orderedClock,
);
$older = $ordered->reserveObservation();
$newer = $ordered->reserveObservation();
healthCheck(is_int($older) && is_int($newer), 'Interleaved observations must reserve');
$ordered->recordSuccess($newer);
$ordered->recordFailure($older, 'transport_error', str_repeat('a', 64));
healthCheck($ordered->status() === 'healthy', 'Older completion must not roll state back');
healthCheck($orderedMail->messages === [], 'No-transition observations must not send mail');

$failure = $ordered->reserveSyntheticFailure();
healthCheck(is_int($failure), 'Synthetic failure must reserve one observation');
$ordered->recordFailure($failure, 'transport_error', str_repeat('a', 64));
healthCheck($ordered->status() === 'degraded', 'Newest double failure must degrade health');
$degraded = json_decode($orderedFs->files[$orderedPath], true, 16, JSON_THROW_ON_ERROR);
healthCheck(array_keys($degraded) === ['schema_version', 'status', 'changed_at', 'next_observation_sequence', 'last_applied_sequence', 'classification', 'message_id_hash'],
    'Degraded state must use the exact key set');
healthCheck($degraded['changed_at'] === '2026-07-14T08:33:55Z',
    'Outage changed_at must be stored as the injected UTC timestamp');
healthCheck(count($orderedMail->messages) === 1 && $orderedAuth->isAuthentic($orderedMail->messages[0]),
    'First outage must send exactly one authenticated email');
$decodedError = decodeHealthWire($orderedMail->messages[0]);
healthCheck($decodedError['subject'] === '【要確認】LINE WORKSメール通知で障害が発生しました'
    && $decodedError['body'] === $realErrorBody
    && ($decodedError['headers']['date'] ?? '') === 'Tue, 14 Jul 2026 17:33:55 +0900',
    'Real outage subject, JST Date, and decoded body must match the approved copy exactly');
$firstChangedAt = $degraded['changed_at'];
$firstClassification = $degraded['classification'];
$repeated = $ordered->reserveObservation();
$ordered->recordFailure($repeated, 'http_error', str_repeat('b', 64));
healthCheck(count($orderedMail->messages) === 1, 'Repeated outage must be suppressed');
$afterRepeatedFailure = json_decode($orderedFs->files[$orderedPath], true, 16, JSON_THROW_ON_ERROR);
healthCheck($afterRepeatedFailure['changed_at'] === $firstChangedAt
    && $afterRepeatedFailure['classification'] === $firstClassification
    && $afterRepeatedFailure['message_id_hash'] === str_repeat('a', 64),
    'Repeated outage must preserve the first changed_at, classification, and hash');
$recovery = $ordered->reserveObservation();
$ordered->recordSuccess($recovery);
healthCheck($ordered->status() === 'healthy' && count($orderedMail->messages) === 2
    && $orderedAuth->isAuthentic($orderedMail->messages[1]), 'First recovery must send one authenticated email');
$decodedRecovery = decodeHealthWire($orderedMail->messages[1]);
healthCheck($decodedRecovery['subject'] === '【復旧・要確認】LINE WORKSメール通知が復旧しました'
    && $decodedRecovery['body'] === $realRecoveryBody
    && ($decodedRecovery['headers']['date'] ?? '') === 'Tue, 14 Jul 2026 17:35:10 +0900',
    'Real recovery subject, JST Date, and decoded body must use the saved outage time exactly');
$healthyAgain = json_decode($orderedFs->files[$orderedPath], true, 16, JSON_THROW_ON_ERROR);
healthCheck(array_keys($healthyAgain) === [
    'schema_version', 'status', 'changed_at', 'next_observation_sequence', 'last_applied_sequence',
] && $healthyAgain['changed_at'] === '2026-07-14T08:35:10Z',
    'Recovery must retain the exact healthy key set and save changed_at in UTC');
$successAgain = $ordered->reserveObservation();
$ordered->recordSuccess($successAgain);
healthCheck(count($orderedMail->messages) === 2, 'Repeated healthy success must be suppressed');
foreach ($orderedMail->messages as $message) {
    $transitionBody = substr($message, strpos($message, "\r\n\r\n") + 4);
    healthCheck(str_ends_with($transitionBody, "\r\n")
        && !str_ends_with($transitionBody, "\r\n\r\n"),
        'Transition producer must supply exactly one terminal CRLF on the wire');
    $decoded = decodeHealthWire($message);
    healthCheck(($decoded['headers']['to'] ?? '') === 'operator@example.invalid',
        'Health wire To header must contain only the configured operator');
    foreach (['ORIGINAL_FROM_MARKER', 'ORIGINAL_TO_MARKER', 'ORIGINAL_CC_MARKER', 'ORIGINAL_BCC_MARKER',
        'ORIGINAL_SUBJECT_MARKER', 'ORIGINAL_BODY_MARKER', 'ORIGINAL_ATTACHMENT_MARKER',
        'EXCEPTION_MARKER', 'https://webhook.example.invalid/PRIVATE_WEBHOOK_MARKER',
        'HMAC_KEY_MARKER', '/private-test/operational.jsonl', str_repeat('a', 64), str_repeat('b', 64)] as $secret) {
        healthCheck(!str_contains($message, $secret) && !str_contains($decoded['body'], $secret),
            'Health wire and decoded body must omit original mail, secrets, hashes, and log paths');
    }
}
unlink($orderedLog);

$testErrorBody = "これは管理者による障害通知メールの動作確認です。\n"
    . "実際の障害ではありません。対応は不要です。\n\n"
    . "テスト実行日時：2026年07月14日（火）17時33分55秒\n"
    . "確認結果：障害通知メールを正常に送信しました。\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：forced_test_failure\n";
$testRecoveryBody = "これは管理者による復旧通知メールの動作確認です。\n"
    . "実際の障害ではありません。対応は不要です。\n\n"
    . "テスト実行日時：2026年07月14日（火）17時35分10秒\n"
    . "確認結果：復旧通知メールを正常に送信しました。\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：forced_test_failure\n";
$testTimes = [
    new DateTimeImmutable('2026-07-13T12:00:00Z'),
    new DateTimeImmutable('2026-07-14T08:33:55Z'),
    new DateTimeImmutable('2026-07-14T08:35:10Z'),
];
$testClock = static function () use (&$testTimes): DateTimeImmutable {
    $time = array_shift($testTimes);
    healthCheck($time instanceof DateTimeImmutable, 'Test transition clock must not be exhausted');
    return $time;
};
[$testMonitor, , $testMail, , $testLog, $testAuth] = fakeMonitor(utcClock: $testClock);
$testFailure = $testMonitor->reserveSyntheticFailure();
healthCheck(is_int($testFailure), 'Synthetic outage must reserve an observation');
$testMonitor->recordFailure($testFailure, 'forced_test_failure', str_repeat('f', 64));
$testSuccess = $testMonitor->reserveObservation();
healthCheck(is_int($testSuccess), 'Synthetic recovery must reserve an observation');
$testMonitor->recordSuccess($testSuccess);
healthCheck(count($testMail->messages) === 2
    && $testAuth->isAuthentic($testMail->messages[0])
    && $testAuth->isAuthentic($testMail->messages[1]),
    'Synthetic outage and recovery must send two authenticated emails');
$decodedTestError = decodeHealthWire($testMail->messages[0]);
$decodedTestRecovery = decodeHealthWire($testMail->messages[1]);
healthCheck($decodedTestError['subject'] === '【テスト・対応不要】障害通知メールの動作確認'
    && $decodedTestError['body'] === $testErrorBody,
    'Test outage subject and decoded body must match the approved copy exactly');
healthCheck($decodedTestRecovery['subject'] === '【テスト・対応不要】復旧通知メールの動作確認'
    && $decodedTestRecovery['body'] === $testRecoveryBody,
    'Test recovery subject and decoded body must match the approved copy exactly');
foreach ($testMail->messages as $message) {
    $decoded = decodeHealthWire($message);
    foreach (['ORIGINAL_FROM_MARKER', 'ORIGINAL_TO_MARKER', 'ORIGINAL_CC_MARKER', 'ORIGINAL_BCC_MARKER',
        'ORIGINAL_SUBJECT_MARKER', 'ORIGINAL_BODY_MARKER', 'ORIGINAL_ATTACHMENT_MARKER',
        'EXCEPTION_MARKER', 'https://webhook.example.invalid/PRIVATE_WEBHOOK_MARKER',
        'HMAC_KEY_MARKER', '/private-test/operational.jsonl', str_repeat('f', 64)] as $secret) {
        healthCheck(!str_contains($message, $secret) && !str_contains($decoded['body'], $secret),
            'Synthetic health wire and decoded body must omit original mail, secrets, hashes, and log paths');
    }
}
unlink($testLog);

foreach (['success', 'system_mail_suppressed', 'not_allowlisted'] as $unsafeClassification) {
    [$classificationMonitor, $classificationFs, $classificationMail, $classificationPath, $classificationLog] = fakeMonitor();
    $classificationSequence = $classificationMonitor->reserveObservation();
    healthCheck(is_int($classificationSequence), 'Classification failure must reserve an observation');
    $classificationMonitor->recordFailure(
        $classificationSequence,
        $unsafeClassification,
        str_repeat('9', 64),
    );
    $classificationState = json_decode(
        $classificationFs->files[$classificationPath], true, 16, JSON_THROW_ON_ERROR,
    );
    $classificationBody = decodeHealthWire($classificationMail->messages[0])['body'];
    healthCheck($classificationState['classification'] === 'unknown'
        && str_contains($classificationBody, "障害内容：原因不明のメール通知エラーです。\n")
        && str_contains($classificationBody, "原因コード：unknown\n")
        && !str_contains($classificationBody, $unsafeClassification),
        'Unsafe new failure classification must degrade to unknown in state and decoded body');
    unlink($classificationLog);
}

[$legacyMonitor, $legacyFs, $legacyMail, $legacyPath, $legacyLog] = fakeMonitor(
    utcClock: static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-14T08:35:10Z'),
);
$legacyFs->files[$legacyPath] = json_encode([
    'schema_version' => 1,
    'status' => 'degraded',
    'changed_at' => '2026-07-14T08:33:55Z',
    'next_observation_sequence' => 2,
    'last_applied_sequence' => 1,
    'classification' => 'system_mail_suppressed',
    'message_id_hash' => str_repeat('8', 64),
], JSON_THROW_ON_ERROR) . "\n";
$legacyMonitor->recordSuccess(2);
$legacyBody = decodeHealthWire($legacyMail->messages[0])['body'];
healthCheck($legacyMonitor->status() === 'healthy'
    && str_contains($legacyBody, "障害内容：原因不明のメール通知エラーです。\n")
    && str_contains($legacyBody, "原因コード：unknown\n")
    && !str_contains($legacyBody, 'system_mail_suppressed'),
    'Legacy suppressed classification must remain readable but display as unknown during recovery');
unlink($legacyLog);

[$failedTransition, $failedFs, $failedAdapter, $failedPath, $failedLog] = fakeMonitor();
$failedAdapter->exitCode = 1;
$failedSequence = $failedTransition->reserveObservation();
$failedTransition->recordFailure($failedSequence, 'transport_error', str_repeat('c', 64));
$afterSendFailure = json_decode($failedFs->files[$failedPath], true, 16, JSON_THROW_ON_ERROR);
healthCheck($afterSendFailure['status'] === 'healthy'
    && $afterSendFailure['last_applied_sequence'] === $failedSequence,
    'Sendmail failure must retain state but advance last_applied_sequence');
$failedAdapter->exitCode = 0;
$retrySequence = $failedTransition->reserveObservation();
$failedTransition->recordFailure($retrySequence, 'transport_error', str_repeat('c', 64));
healthCheck($failedTransition->status() === 'degraded' && count($failedAdapter->messages) === 2,
    'Next new sequence must retry a failed transition email');
unlink($failedLog);

$failedRecoveryTimes = [
    new DateTimeImmutable('2026-07-13T12:00:00Z'),
    new DateTimeImmutable('2026-07-14T08:33:55Z'),
    new DateTimeImmutable('2026-07-14T08:35:10Z'),
    new DateTimeImmutable('2026-07-14T08:36:10Z'),
];
$failedRecoveryClock = static function () use (&$failedRecoveryTimes): DateTimeImmutable {
    $time = array_shift($failedRecoveryTimes);
    healthCheck($time instanceof DateTimeImmutable, 'Failed recovery clock must not be exhausted');
    return $time;
};
[$failedRecovery, $failedRecoveryFs, $failedRecoveryAdapter, $failedRecoveryPath, $failedRecoveryLog] = fakeMonitor(
    utcClock: $failedRecoveryClock,
);
$failedRecoveryFailure = $failedRecovery->reserveObservation();
$failedRecovery->recordFailure($failedRecoveryFailure, 'transport_error', str_repeat('7', 64));
$failedRecoveryAdapter->exitCode = 1;
$failedRecoverySequence = $failedRecovery->reserveObservation();
$failedRecovery->recordSuccess($failedRecoverySequence);
$afterRecoverySendFailure = json_decode(
    $failedRecoveryFs->files[$failedRecoveryPath], true, 16, JSON_THROW_ON_ERROR,
);
healthCheck($afterRecoverySendFailure['status'] === 'degraded'
    && $afterRecoverySendFailure['changed_at'] === '2026-07-14T08:33:55Z'
    && $afterRecoverySendFailure['last_applied_sequence'] === $failedRecoverySequence,
    'Recovery sendmail failure must retain degraded state and outage time but advance the sequence');
$failedRecoveryAdapter->exitCode = 0;
$recoveryRetrySequence = $failedRecovery->reserveObservation();
$failedRecovery->recordSuccess($recoveryRetrySequence);
healthCheck($failedRecovery->status() === 'healthy' && count($failedRecoveryAdapter->messages) === 3,
    'Next new success observation must retry a failed recovery email');
unlink($failedRecoveryLog);

[$crashMonitor, $crashFs, $crashMail, , $crashLog] = fakeMonitor();
$crashSequence = $crashMonitor->reserveObservation();
$crashFs->faults[] = 'post_replace';
$crashMonitor->recordFailure($crashSequence, 'transport_error', str_repeat('d', 64));
healthCheck(count($crashMail->messages) === 1, 'Crash-window fixture must send before state commit failure');
$crashRetry = $crashMonitor->reserveObservation();
$crashMonitor->recordFailure($crashRetry, 'transport_error', str_repeat('d', 64));
healthCheck(count($crashMail->messages) === 2,
    'Only sendmail-success/state-commit crash window may produce at-least-once duplicate');
unlink($crashLog);

$nativeDirectory = sys_get_temp_dir() . '/health-native-' . bin2hex(random_bytes(8));
mkdir($nativeDirectory, 0700);
$nativeDirectory = realpath($nativeDirectory);
healthCheck(is_string($nativeDirectory), 'Native fixture directory must resolve');
$nativePath = $nativeDirectory . '/delivery-health.json';
$nativeLog = $nativeDirectory . '/operational.jsonl';
$nativeAdapter = new HealthSendmailAdapter();
$nativeMonitor = new DeliveryHealthMonitor(
    $nativePath, ['operator@example.invalid'], $nativeLog,
    new SystemMailAuthenticator(str_repeat('n', 32)),
    new SendmailClient($nativeAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($nativeLog), new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('a', 32),
        null,
        static fn (): array => ['home' => dirname($nativeDirectory), 'uid' => posix_geteuid()],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('f', 32),
);
healthCheck($nativeMonitor->reserveObservation() === 1, 'Native strict filesystem must initialize and reserve');
healthCheck((fileperms($nativePath) & 0777) === 0600
    && (fileperms($nativeDirectory . '/.delivery-health.lock') & 0777) === 0600,
    'Native state and sibling lock must be mode 0600');
$ambiguousReadSucceeded = false;
$ambiguousFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('0', 32),
    null,
    static fn (): array => ['home' => dirname($nativeDirectory), 'uid' => posix_geteuid()],
);
try {
    $ambiguousFilesystem->withExclusiveLock(
        $nativeDirectory . '/.delivery-health.lock',
        static function () use ($ambiguousFilesystem, $nativeDirectory, &$ambiguousReadSucceeded): void {
            $ambiguousFilesystem->readRegular($nativeDirectory . '//delivery-health.json', 4096);
            $ambiguousReadSucceeded = true;
        },
    );
} catch (RuntimeException) {
    // Ambiguous state paths must be rejected.
}
healthCheck(!$ambiguousReadSucceeded, 'A noncanonical state path containing a double slash must be rejected');
chmod($nativePath, 0644);
healthCheck($nativeMonitor->reserveObservation() === null, 'Native mode mismatch must fail closed for state changes');
chmod($nativePath, 0600);
unlink($nativePath);
$target = $nativeDirectory . '/target.json';
file_put_contents($target, "{}\n"); chmod($target, 0600);
symlink($target, $nativePath);
healthCheck($nativeMonitor->reserveObservation() === null, 'Native symlink state must be rejected');
unlink($nativePath); unlink($target);
foreach (glob($nativeDirectory . '/*') ?: [] as $file) { if (is_file($file)) unlink($file); }
foreach (glob($nativeDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
rmdir($nativeDirectory);

$replaceRaceDirectory = sys_get_temp_dir() . '/health-directory-replace-' . bin2hex(random_bytes(8));
mkdir($replaceRaceDirectory, 0700);
$replaceRaceDirectory = realpath($replaceRaceDirectory);
healthCheck(is_string($replaceRaceDirectory), 'Replace-race directory must resolve');
$replaceRaceMoved = $replaceRaceDirectory . '-moved';
$replaceRaceLog = $replaceRaceDirectory . '/operational.jsonl';
$replaceRaceAdapter = new HealthSendmailAdapter();
$replaceHookUsed = false;
$replaceRaceFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('b', 32),
    static function (string $checkpoint) use (
        &$replaceHookUsed, $replaceRaceDirectory, $replaceRaceMoved,
    ): void {
        if ($checkpoint !== 'before_replace' || $replaceHookUsed) {
            return;
        }
        $replaceHookUsed = true;
        rename($replaceRaceDirectory, $replaceRaceMoved);
        mkdir($replaceRaceDirectory, 0700);
    },
    static fn (): array => ['home' => dirname($replaceRaceDirectory), 'uid' => posix_geteuid()],
);
$replaceRaceMonitor = new DeliveryHealthMonitor(
    $replaceRaceDirectory . '/delivery-health.json', ['operator@example.invalid'], $replaceRaceLog,
    new SystemMailAuthenticator(str_repeat('r', 32)),
    new SendmailClient($replaceRaceAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($replaceRaceLog), $replaceRaceFilesystem,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('1', 32),
);
healthCheck($replaceRaceMonitor->reserveObservation() === null,
    'Directory replacement before atomic state write must not report a successful reservation');
healthCheck($replaceHookUsed && !file_exists($replaceRaceDirectory . '/delivery-health.json')
    && !file_exists($replaceRaceMoved . '/delivery-health.json'),
    'Directory replacement must not accept a state write in either directory identity');
healthCheck($replaceRaceAdapter->messages === []
    && str_contains((string) file_get_contents($replaceRaceLog), 'health_state_failure'),
    'Directory replacement during reservation must emit only health_state_failure');

$transitionRaceDirectory = sys_get_temp_dir() . '/health-directory-transition-' . bin2hex(random_bytes(8));
mkdir($transitionRaceDirectory, 0700);
$transitionRaceDirectory = realpath($transitionRaceDirectory);
healthCheck(is_string($transitionRaceDirectory), 'Transition-race directory must resolve');
$transitionRaceMoved = $transitionRaceDirectory . '-moved';
$transitionRacePath = $transitionRaceDirectory . '/delivery-health.json';
$transitionRaceLog = $transitionRaceDirectory . '/operational.jsonl';
$transitionBytes = json_encode([
    'schema_version' => 1, 'status' => 'healthy',
    'changed_at' => '2026-07-13T12:00:00Z',
    'next_observation_sequence' => 1, 'last_applied_sequence' => 0,
], JSON_THROW_ON_ERROR) . "\n";
file_put_contents($transitionRacePath, $transitionBytes);
chmod($transitionRacePath, 0600);
$transitionRaceAdapter = new HealthSendmailAdapter();
$transitionHookUsed = false;
$transitionRaceFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('c', 32),
    static function (string $checkpoint) use (
        &$transitionHookUsed, $transitionRaceDirectory, $transitionRaceMoved,
    ): void {
        if ($checkpoint !== 'locked' || $transitionHookUsed) {
            return;
        }
        $transitionHookUsed = true;
        rename($transitionRaceDirectory, $transitionRaceMoved);
        mkdir($transitionRaceDirectory, 0700);
    },
    static fn (): array => ['home' => dirname($transitionRaceDirectory), 'uid' => posix_geteuid()],
);
$transitionRaceMonitor = new DeliveryHealthMonitor(
    $transitionRacePath, ['operator@example.invalid'], $transitionRaceLog,
    new SystemMailAuthenticator(str_repeat('t', 32)),
    new SendmailClient($transitionRaceAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($transitionRaceLog), $transitionRaceFilesystem,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('2', 32),
);
$transitionRaceMonitor->recordFailure(1, 'transport_error', str_repeat('d', 64));
healthCheck($transitionHookUsed && $transitionRaceAdapter->messages === [],
    'Directory replacement before transition read must suppress outage notification');
healthCheck((string) file_get_contents($transitionRaceMoved . '/delivery-health.json') === $transitionBytes
    && !file_exists($transitionRaceDirectory . '/delivery-health.json'),
    'Directory replacement must leave the prior state unchanged and create no replacement state');
healthCheck(str_contains((string) file_get_contents($transitionRaceLog), 'health_state_failure'),
    'Directory replacement during transition must log only health_state_failure');

$splitLockDirectory = sys_get_temp_dir() . '/health-split-lock-' . bin2hex(random_bytes(8));
mkdir($splitLockDirectory, 0700);
$splitLockDirectory = realpath($splitLockDirectory);
healthCheck(is_string($splitLockDirectory), 'Split-lock directory must resolve');
$splitLockPath = $splitLockDirectory . '/.delivery-health.lock';
$splitStatePath = $splitLockDirectory . '/delivery-health.json';
$splitLog = $splitLockDirectory . '/operational.jsonl';
file_put_contents($splitStatePath, $transitionBytes); chmod($splitStatePath, 0600);
$splitAdapter = new HealthSendmailAdapter();
$splitHookUsed = false;
$replacementCallbackReached = false;
$splitFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('d', 32),
    static function (string $checkpoint) use (
        &$splitHookUsed, &$replacementCallbackReached, $splitLockPath, $splitLockDirectory,
    ): void {
        if ($checkpoint !== 'before_lock_lease_assert' || $splitHookUsed) return;
        $splitHookUsed = true;
        $saved = $splitLockPath . '.saved';
        rename($splitLockPath, $saved);
        file_put_contents($splitLockPath, ''); chmod($splitLockPath, 0600);
        $replacementFilesystem = new NativePrivateStateFilesystem(
            null, null,
            static fn (): array => ['home' => dirname($splitLockDirectory), 'uid' => posix_geteuid()],
        );
        try {
            $replacementFilesystem->withExclusiveLock($splitLockPath,
                static function () use (&$replacementCallbackReached): void {
                    $replacementCallbackReached = true;
                });
        } catch (RuntimeException) {
            // The retained directory-inode lock must reject the replacement lock holder.
        }
    },
    static fn (): array => ['home' => dirname($splitLockDirectory), 'uid' => posix_geteuid()],
);
$splitMonitor = new DeliveryHealthMonitor(
    $splitStatePath, ['operator@example.invalid'], $splitLog,
    new SystemMailAuthenticator(str_repeat('u', 32)),
    new SendmailClient($splitAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($splitLog), $splitFilesystem,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('3', 32),
);
$splitMonitor->recordFailure(1, 'transport_error', str_repeat('e', 64));
healthCheck($splitHookUsed && !$replacementCallbackReached && $splitAdapter->messages === [],
    'Neither side of a replaced health lock may reach callback/sendmail after the split');
healthCheck((string) file_get_contents($splitStatePath) === $transitionBytes,
    'Health split-lock detection must leave state uncommitted');

$postCallbackDirectory = sys_get_temp_dir() . '/health-directory-post-callback-' . bin2hex(random_bytes(8));
mkdir($postCallbackDirectory, 0700);
$postCallbackDirectory = realpath($postCallbackDirectory);
healthCheck(is_string($postCallbackDirectory), 'Post-callback directory must resolve');
$postCallbackMoved = $postCallbackDirectory . '-moved';
$postCallbackFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('e', 32),
    null,
    static fn (): array => ['home' => dirname($postCallbackDirectory), 'uid' => posix_geteuid()],
);
$postCallbackReportedSuccess = false;
try {
    $postCallbackFilesystem->withExclusiveLock(
        $postCallbackDirectory . '/.delivery-health.lock',
        static function () use (
            $postCallbackFilesystem, $postCallbackDirectory, $postCallbackMoved,
        ): string {
            $postCallbackFilesystem->replaceAtomic(
                $postCallbackDirectory . '/delivery-health.json', "{}\n", 0600,
            );
            rename($postCallbackDirectory, $postCallbackMoved);
            mkdir($postCallbackDirectory, 0700);
            return 'callback-success';
        },
    );
    $postCallbackReportedSuccess = true;
} catch (RuntimeException) {
    // The post-callback directory identity barrier must reject success.
}
healthCheck(!$postCallbackReportedSuccess
    && file_exists($postCallbackMoved . '/delivery-health.json')
    && !file_exists($postCallbackDirectory . '/delivery-health.json'),
    'Post-callback rename must fail the operation and never attribute the old locked write to the replacement');

foreach ([$replaceRaceDirectory, $replaceRaceMoved, $transitionRaceDirectory, $transitionRaceMoved,
    $splitLockDirectory,
    $postCallbackDirectory, $postCallbackMoved]
    as $raceCleanupDirectory) {
    foreach (glob($raceCleanupDirectory . '/*') ?: [] as $file) { if (is_file($file)) unlink($file); }
    foreach (glob($raceCleanupDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
    rmdir($raceCleanupDirectory);
}

$untrustedHome = sys_get_temp_dir() . '/health-untrusted-chain-' . bin2hex(random_bytes(8));
mkdir($untrustedHome, 0700);
$untrustedHome = realpath($untrustedHome);
healthCheck(is_string($untrustedHome), 'Untrusted-chain home must resolve');
$writableAncestor = $untrustedHome . '/writable';
mkdir($writableAncestor, 0700);
chmod($writableAncestor, 0777);
$untrustedDirectory = $writableAncestor . '/health';
mkdir($untrustedDirectory, 0700);
$untrustedLog = $untrustedDirectory . '/operational.jsonl';
$untrustedAdapter = new HealthSendmailAdapter();
$untrustedMonitor = new DeliveryHealthMonitor(
    $untrustedDirectory . '/delivery-health.json', ['operator@example.invalid'], $untrustedLog,
    new SystemMailAuthenticator(str_repeat('u', 32)),
    new SendmailClient($untrustedAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($untrustedLog),
    new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('6', 32),
        null,
        static fn (): array => ['home' => $untrustedHome, 'uid' => posix_geteuid()],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('6', 32),
);
healthCheck($untrustedMonitor->reserveObservation() === null
    && $untrustedAdapter->messages === []
    && !file_exists($untrustedDirectory . '/delivery-health.json'),
    'A mode-0777 private ancestor must suppress state and mail');
healthCheck(str_contains((string) file_get_contents($untrustedLog), 'health_state_failure'),
    'A mode-0777 private ancestor must emit health_state_failure');

$symlinkHome = sys_get_temp_dir() . '/health-symlink-chain-' . bin2hex(random_bytes(8));
mkdir($symlinkHome, 0700);
$symlinkHome = realpath($symlinkHome);
healthCheck(is_string($symlinkHome), 'Symlink-chain home must resolve');
mkdir($symlinkHome . '/real', 0700);
mkdir($symlinkHome . '/real/health', 0700);
symlink($symlinkHome . '/real', $symlinkHome . '/alias');
$symlinkDirectory = $symlinkHome . '/alias/health';
$symlinkLog = $symlinkDirectory . '/operational.jsonl';
$symlinkAdapter = new HealthSendmailAdapter();
$symlinkMonitor = new DeliveryHealthMonitor(
    $symlinkDirectory . '/delivery-health.json', ['operator@example.invalid'], $symlinkLog,
    new SystemMailAuthenticator(str_repeat('s', 32)),
    new SendmailClient($symlinkAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($symlinkLog),
    new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('7', 32),
        null,
        static fn (): array => ['home' => $symlinkHome, 'uid' => posix_geteuid()],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('7', 32),
);
healthCheck($symlinkMonitor->reserveObservation() === null
    && $symlinkAdapter->messages === []
    && !file_exists($symlinkDirectory . '/delivery-health.json'),
    'A symlinked private ancestor must suppress state and mail');
healthCheck(str_contains((string) file_get_contents($symlinkLog), 'health_state_failure'),
    'A symlinked private ancestor must emit health_state_failure');

$wrongOwnerHome = sys_get_temp_dir() . '/health-wrong-owner-chain-' . bin2hex(random_bytes(8));
mkdir($wrongOwnerHome, 0700);
$wrongOwnerHome = realpath($wrongOwnerHome);
healthCheck(is_string($wrongOwnerHome), 'Wrong-owner-chain home must resolve');
mkdir($wrongOwnerHome . '/health', 0700);
$wrongOwnerDirectory = $wrongOwnerHome . '/health';
$wrongOwnerLog = $wrongOwnerDirectory . '/operational.jsonl';
$wrongOwnerAdapter = new HealthSendmailAdapter();
$wrongOwnerMonitor = new DeliveryHealthMonitor(
    $wrongOwnerDirectory . '/delivery-health.json', ['operator@example.invalid'], $wrongOwnerLog,
    new SystemMailAuthenticator(str_repeat('w', 32)),
    new SendmailClient($wrongOwnerAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($wrongOwnerLog),
    new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('8', 32),
        null,
        static fn (): array => ['home' => $wrongOwnerHome, 'uid' => posix_geteuid() + 1],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('8', 32),
);
healthCheck($wrongOwnerMonitor->reserveObservation() === null
    && $wrongOwnerAdapter->messages === []
    && !file_exists($wrongOwnerDirectory . '/delivery-health.json'),
    'An injected wrong-owner chain must suppress state and mail');
healthCheck(str_contains((string) file_get_contents($wrongOwnerLog), 'health_state_failure'),
    'An injected wrong-owner chain must emit health_state_failure');

$ancestorRaceHome = sys_get_temp_dir() . '/health-ancestor-race-' . bin2hex(random_bytes(8));
mkdir($ancestorRaceHome, 0700);
$ancestorRaceHome = realpath($ancestorRaceHome);
healthCheck(is_string($ancestorRaceHome), 'Ancestor-race home must resolve');
$ancestorRaceDirectory = $ancestorRaceHome . '/private';
mkdir($ancestorRaceDirectory, 0700);
mkdir($ancestorRaceDirectory . '/health', 0700);
$ancestorRaceMoved = $ancestorRaceHome . '/private-moved';
$ancestorRaceLog = $ancestorRaceDirectory . '/health/operational.jsonl';
$ancestorRaceAdapter = new HealthSendmailAdapter();
$ancestorRaceHookUsed = false;
$ancestorRaceFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('9', 32),
    static function (string $checkpoint) use (
        &$ancestorRaceHookUsed, $ancestorRaceDirectory, $ancestorRaceMoved,
    ): void {
        if ($checkpoint !== 'before_replace' || $ancestorRaceHookUsed) {
            return;
        }
        $ancestorRaceHookUsed = true;
        rename($ancestorRaceDirectory, $ancestorRaceMoved);
        mkdir($ancestorRaceDirectory, 0700);
        mkdir($ancestorRaceDirectory . '/health', 0700);
    },
    static fn (): array => ['home' => $ancestorRaceHome, 'uid' => posix_geteuid()],
);
$ancestorRaceMonitor = new DeliveryHealthMonitor(
    $ancestorRaceDirectory . '/health/delivery-health.json', ['operator@example.invalid'], $ancestorRaceLog,
    new SystemMailAuthenticator(str_repeat('a', 32)),
    new SendmailClient($ancestorRaceAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($ancestorRaceLog), $ancestorRaceFilesystem,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('9', 32),
);
healthCheck($ancestorRaceMonitor->reserveObservation() === null
    && $ancestorRaceHookUsed && $ancestorRaceAdapter->messages === []
    && !file_exists($ancestorRaceDirectory . '/health/delivery-health.json')
    && !file_exists($ancestorRaceMoved . '/health/delivery-health.json'),
    'Ancestor replacement during state replace must suppress state and mail');
healthCheck(str_contains((string) file_get_contents($ancestorRaceLog), 'health_state_failure'),
    'Ancestor replacement during state replace must emit health_state_failure');

$ancestorReadHome = sys_get_temp_dir() . '/health-ancestor-read-' . bin2hex(random_bytes(8));
mkdir($ancestorReadHome, 0700);
$ancestorReadHome = realpath($ancestorReadHome);
healthCheck(is_string($ancestorReadHome), 'Ancestor-read home must resolve');
$ancestorReadDirectory = $ancestorReadHome . '/private';
mkdir($ancestorReadDirectory, 0700);
mkdir($ancestorReadDirectory . '/health', 0700);
$ancestorReadMoved = $ancestorReadHome . '/private-moved';
$ancestorReadPath = $ancestorReadDirectory . '/health/delivery-health.json';
$ancestorReadBytes = json_encode([
    'schema_version' => 1, 'status' => 'healthy', 'changed_at' => '2026-07-13T12:00:00Z',
    'next_observation_sequence' => 0, 'last_applied_sequence' => 0,
], JSON_THROW_ON_ERROR) . "\n";
file_put_contents($ancestorReadPath, $ancestorReadBytes);
chmod($ancestorReadPath, 0600);
$ancestorReadLog = $ancestorReadDirectory . '/health/operational.jsonl';
$ancestorReadAdapter = new HealthSendmailAdapter();
$ancestorReadHookUsed = false;
$ancestorReadFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('b', 32),
    static function (string $checkpoint) use (
        &$ancestorReadHookUsed, $ancestorReadDirectory, $ancestorReadMoved,
    ): void {
        if ($checkpoint !== 'before_read' || $ancestorReadHookUsed) {
            return;
        }
        $ancestorReadHookUsed = true;
        rename($ancestorReadDirectory, $ancestorReadMoved);
        mkdir($ancestorReadDirectory, 0700);
        mkdir($ancestorReadDirectory . '/health', 0700);
    },
    static fn (): array => ['home' => $ancestorReadHome, 'uid' => posix_geteuid()],
);
$ancestorReadMonitor = new DeliveryHealthMonitor(
    $ancestorReadPath, ['operator@example.invalid'], $ancestorReadLog,
    new SystemMailAuthenticator(str_repeat('b', 32)),
    new SendmailClient($ancestorReadAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($ancestorReadLog), $ancestorReadFilesystem,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('b', 32),
);
healthCheck($ancestorReadMonitor->reserveObservation() === null
    && $ancestorReadHookUsed && $ancestorReadAdapter->messages === []
    && (string) file_get_contents($ancestorReadMoved . '/health/delivery-health.json') === $ancestorReadBytes
    && !file_exists($ancestorReadDirectory . '/health/delivery-health.json'),
    'Ancestor replacement during state read must suppress state and mail');
healthCheck(str_contains((string) file_get_contents($ancestorReadLog), 'health_state_failure'),
    'Ancestor replacement during state read must emit health_state_failure');

$ancestorTransitionHome = sys_get_temp_dir() . '/health-ancestor-transition-' . bin2hex(random_bytes(8));
mkdir($ancestorTransitionHome, 0700);
$ancestorTransitionHome = realpath($ancestorTransitionHome);
healthCheck(is_string($ancestorTransitionHome), 'Ancestor-transition home must resolve');
$ancestorTransitionDirectory = $ancestorTransitionHome . '/private';
mkdir($ancestorTransitionDirectory, 0700);
mkdir($ancestorTransitionDirectory . '/health', 0700);
$ancestorTransitionMoved = $ancestorTransitionHome . '/private-moved';
$ancestorTransitionPath = $ancestorTransitionDirectory . '/health/delivery-health.json';
$ancestorTransitionBytes = json_encode([
    'schema_version' => 1, 'status' => 'healthy', 'changed_at' => '2026-07-13T12:00:00Z',
    'next_observation_sequence' => 1, 'last_applied_sequence' => 0,
], JSON_THROW_ON_ERROR) . "\n";
file_put_contents($ancestorTransitionPath, $ancestorTransitionBytes);
chmod($ancestorTransitionPath, 0600);
$ancestorTransitionLog = $ancestorTransitionDirectory . '/health/operational.jsonl';
$ancestorTransitionAdapter = new HealthSendmailAdapter();
$ancestorTransitionHookUsed = false;
$ancestorTransitionFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('c', 32),
    static function (string $checkpoint) use (
        &$ancestorTransitionHookUsed, $ancestorTransitionDirectory, $ancestorTransitionMoved,
    ): void {
        if ($checkpoint !== 'locked' || $ancestorTransitionHookUsed) {
            return;
        }
        $ancestorTransitionHookUsed = true;
        rename($ancestorTransitionDirectory, $ancestorTransitionMoved);
        mkdir($ancestorTransitionDirectory, 0700);
        mkdir($ancestorTransitionDirectory . '/health', 0700);
    },
    static fn (): array => ['home' => $ancestorTransitionHome, 'uid' => posix_geteuid()],
);
$ancestorTransitionMonitor = new DeliveryHealthMonitor(
    $ancestorTransitionPath, ['operator@example.invalid'], $ancestorTransitionLog,
    new SystemMailAuthenticator(str_repeat('c', 32)),
    new SendmailClient($ancestorTransitionAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($ancestorTransitionLog), $ancestorTransitionFilesystem,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('c', 32),
);
$ancestorTransitionMonitor->recordFailure(1, 'transport_error', str_repeat('c', 64));
healthCheck($ancestorTransitionHookUsed && $ancestorTransitionAdapter->messages === []
    && (string) file_get_contents($ancestorTransitionMoved . '/health/delivery-health.json') === $ancestorTransitionBytes
    && !file_exists($ancestorTransitionDirectory . '/health/delivery-health.json'),
    'Ancestor replacement during a transition must suppress state and mail');
healthCheck(str_contains((string) file_get_contents($ancestorTransitionLog), 'health_state_failure'),
    'Ancestor replacement during a transition must emit health_state_failure');

foreach ([$untrustedDirectory, $symlinkHome . '/real/health', $wrongOwnerDirectory,
    $ancestorRaceDirectory . '/health', $ancestorRaceMoved . '/health',
    $ancestorReadDirectory . '/health', $ancestorReadMoved . '/health',
    $ancestorTransitionDirectory . '/health', $ancestorTransitionMoved . '/health'] as $chainCleanupDirectory) {
    foreach (glob($chainCleanupDirectory . '/*') ?: [] as $file) { if (is_file($file)) unlink($file); }
    foreach (glob($chainCleanupDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
    rmdir($chainCleanupDirectory);
}
chmod($writableAncestor, 0700);
rmdir($writableAncestor);
rmdir($untrustedHome);
unlink($symlinkHome . '/alias');
rmdir($symlinkHome . '/real');
rmdir($symlinkHome);
rmdir($wrongOwnerHome);
rmdir($ancestorRaceDirectory);
rmdir($ancestorRaceMoved);
rmdir($ancestorRaceHome);
rmdir($ancestorReadDirectory);
rmdir($ancestorReadMoved);
rmdir($ancestorReadHome);
rmdir($ancestorTransitionDirectory);
rmdir($ancestorTransitionMoved);
rmdir($ancestorTransitionHome);

$recursiveHome = sys_get_temp_dir() . '/health-recursive-lock-' . bin2hex(random_bytes(8));
mkdir($recursiveHome, 0700);
$recursiveHome = realpath($recursiveHome);
healthCheck(is_string($recursiveHome), 'Recursive-lock home must resolve');
mkdir($recursiveHome . '/health', 0700);
$recursiveDirectory = $recursiveHome . '/health';
$recursiveLock = $recursiveDirectory . '/.delivery-health.lock';
$recursiveBoundaryCount = 0;
$recursiveFilesystem = new NativePrivateStateFilesystem(
    static fn (): string => str_repeat('d', 32),
    static function (string $checkpoint) use (&$recursiveBoundaryCount): void {
        if ($checkpoint === 'before_lock_open') {
            ++$recursiveBoundaryCount;
        }
    },
    static fn (): array => ['home' => $recursiveHome, 'uid' => posix_geteuid()],
);
healthCheck(function_exists('pcntl_alarm') && function_exists('pcntl_signal')
    && function_exists('pcntl_async_signals') && function_exists('pcntl_signal_get_handler'),
    'Recursive-lock regression requires bounded signal support');
$recursiveAlarmFired = false;
$recursivePriorAsyncSignals = pcntl_async_signals(true);
$recursivePriorAlarmHandler = pcntl_signal_get_handler(SIGALRM);
pcntl_signal(SIGALRM, static function () use (&$recursiveAlarmFired): never {
    $recursiveAlarmFired = true;
    throw new RuntimeException('RECURSIVE_LOCK_ALARM');
});
$recursiveInnerMessage = null;
$recursiveOuterResult = null;
pcntl_alarm(1);
try {
    $recursiveOuterResult = $recursiveFilesystem->withExclusiveLock(
        $recursiveLock,
        static function () use ($recursiveFilesystem, $recursiveLock, &$recursiveInnerMessage): string {
            try {
                $recursiveFilesystem->withExclusiveLock($recursiveLock, static fn (): string => 'unreachable');
            } catch (RuntimeException $exception) {
                $recursiveInnerMessage = $exception->getMessage();
            }
            return 'outer-success';
        },
    );
} finally {
    pcntl_alarm(0);
    pcntl_signal(SIGALRM, $recursivePriorAlarmHandler);
    pcntl_async_signals($recursivePriorAsyncSignals);
}
healthCheck(!$recursiveAlarmFired && $recursiveInnerMessage === 'Private state unavailable'
    && $recursiveOuterResult === 'outer-success' && $recursiveBoundaryCount === 1,
    'Same-instance recursive lock must fail before a second lock-open boundary without disturbing the outer operation');
$recursiveFailureCaught = false;
try {
    $recursiveFilesystem->withExclusiveLock(
        $recursiveLock,
        static function (): never { throw new RuntimeException('OUTER_FAILURE_MARKER'); },
    );
} catch (RuntimeException $exception) {
    $recursiveFailureCaught = $exception->getMessage() === 'OUTER_FAILURE_MARKER';
}
$recursiveReuse = $recursiveFilesystem->withExclusiveLock(
    $recursiveLock, static fn (): string => 'sequential-success',
);
healthCheck($recursiveFailureCaught && $recursiveReuse === 'sequential-success' && $recursiveBoundaryCount === 3
    && !file_exists($recursiveDirectory . '/delivery-health.json'),
    'Active lock state must reset after successful and failed attempts and permit sequential reuse without state mutation');
unlink($recursiveLock);
rmdir($recursiveDirectory);
rmdir($recursiveHome);

fwrite(STDOUT, "PASS: delivery health state machine and Japanese alert contract\n");
