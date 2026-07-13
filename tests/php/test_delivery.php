<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';
require_once dirname(__DIR__, 2) . '/src/SendmailProcessAdapter.php';

use XserverMail\DeliveryHealthMonitor;
use XserverMail\ErrorReporter;
use XserverMail\DeliveryApplication;
use XserverMail\DeliveryDeduplicator;
use XserverMail\NotifierConfig;
use XserverMail\OperationalLogger;
use XserverMail\NativePrivateStateFilesystem;
use XserverMail\SendmailClient;
use XserverMail\SendmailProcessAdapter;
use XserverMail\SendmailProcessHandle;
use XserverMail\SystemMailAuthenticator;
use XserverMail\WebhookClient;
use XserverMail\StdinFrame;

function deliveryCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

/** @return list<string> */
function boundaryAddresses(int $count, ?int $toBytes = null): array
{
    $localLengths = array_fill(0, $count, 2);
    if ($toBytes !== null) {
        $localTotal = $toBytes - ($count * 16) - max(0, $count - 1);
        $localLengths = array_fill(0, $count - 1, 10);
        $localLengths[] = $localTotal - array_sum($localLengths);
    }
    $addresses = [];
    foreach ($localLengths as $index => $length) {
        $prefix = chr(97 + intdiv($index, 26)) . chr(97 + ($index % 26));
        $addresses[] = $prefix . str_repeat('x', $length - 2) . '@example.invalid';
    }
    return $addresses;
}

$configKey = rtrim(strtr(base64_encode(str_repeat('k', 32)), '+/', '-_'), '=');
$canonicalConfig = NotifierConfig::fromArray([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['CaseSensitive@example.invalid'],
    'notification_pinned_targets' => ['CaseSensitive@example.invalid'],
    'notification_targets' => ['CaseSensitive@example.invalid'],
    'system_mail_hmac_key' => $configKey,
    'log_path' => '/tmp/notifier.log',
]);
deliveryCheck($canonicalConfig->systemMailHmacKey === str_repeat('k', 32),
    'HMAC key must strict-decode to 32 bytes');
deliveryCheck($canonicalConfig->healthPath === '/tmp/delivery-health.json',
    'Health state path must be derived beside the log');
deliveryCheck(is_int($canonicalConfig->worstCaseHeaderLineBytes)
    && $canonicalConfig->worstCaseHeaderLineBytes < 998,
    'Accepted config must prove every unfolded header is below 998 bytes');
deliveryCheck(is_int($canonicalConfig->worstCaseSignedMessageBytes)
    && $canonicalConfig->worstCaseSignedMessageBytes < 65536,
    'Accepted config must prove the complete signed message is below 65536 bytes');
foreach ([
    array_replace(['webhook_url' => 'https://webhook.worksmobile.com/message/test',
        'error_recipients' => ['CaseSensitive@example.invalid'],
        'notification_pinned_targets' => ['CaseSensitive@example.invalid'],
        'notification_targets' => ['CaseSensitive@example.invalid'],
        'system_mail_hmac_key' => $configKey, 'log_path' => '/tmp/notifier.log'],
        ['system_mail_hmac_key' => str_repeat('A', 42)]),
    array_replace(['webhook_url' => 'https://webhook.worksmobile.com/message/test',
        'error_recipients' => ['CaseSensitive@example.invalid'],
        'notification_pinned_targets' => ['CaseSensitive@example.invalid'],
        'notification_targets' => ['CaseSensitive@example.invalid'],
        'system_mail_hmac_key' => $configKey, 'log_path' => '/tmp/notifier.log'],
        ['notification_pinned_targets' => ['CaseSensitive@EXAMPLE.INVALID']]),
    array_replace(['webhook_url' => 'https://webhook.worksmobile.com/message/test',
        'error_recipients' => ['CaseSensitive@example.invalid'],
        'notification_pinned_targets' => ['CaseSensitive@example.invalid'],
        'notification_targets' => ['CaseSensitive@example.invalid'],
        'system_mail_hmac_key' => $configKey, 'log_path' => '/tmp/notifier.log'],
        ['notification_pinned_targets' => ['Name <x@example.invalid>']]),
] as $invalidCanonicalConfig) {
    try {
        NotifierConfig::fromArray($invalidCanonicalConfig);
        throw new RuntimeException('Invalid canonical config was accepted');
    } catch (InvalidArgumentException) {
        // Expected.
    }
}

foreach ([
    ['count-32', boundaryAddresses(32), '/tmp/notifier.log', true],
    ['count-33', boundaryAddresses(33), '/tmp/notifier.log', false],
    ['to-900', boundaryAddresses(32, 900), '/tmp/notifier.log', true],
    ['to-901', boundaryAddresses(32, 901), '/tmp/notifier.log', false],
    ['log-4096', ['operator@example.invalid'], '/' . str_repeat('a', 4095), true],
    ['log-4097', ['operator@example.invalid'], '/' . str_repeat('a', 4096), false],
] as [$boundaryName, $boundaryRecipients, $boundaryLogPath, $boundaryAccepted]) {
    $boundaryInput = [
        'webhook_url' => 'https://webhook.worksmobile.com/message/test',
        'error_recipients' => $boundaryRecipients,
        'notification_pinned_targets' => [], 'notification_targets' => [],
        'system_mail_hmac_key' => $configKey, 'log_path' => $boundaryLogPath,
    ];
    try {
        $boundaryConfig = NotifierConfig::fromArray($boundaryInput);
        deliveryCheck($boundaryAccepted, $boundaryName . ' must be rejected');
        deliveryCheck($boundaryConfig->worstCaseHeaderLineBytes < 998,
            $boundaryName . ' header formula must remain below 998 bytes');
        deliveryCheck($boundaryConfig->worstCaseSignedMessageBytes < 65536,
            $boundaryName . ' signed-message formula must remain below 65536 bytes');
    } catch (InvalidArgumentException) {
        deliveryCheck(!$boundaryAccepted, $boundaryName . ' must be accepted');
    }
}
$manyNotificationTargets = boundaryAddresses(40);
$targetHeavyConfig = NotifierConfig::fromArray([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['operator@example.invalid'],
    'notification_pinned_targets' => $manyNotificationTargets,
    'notification_targets' => $manyNotificationTargets,
    'system_mail_hmac_key' => $configKey,
    'log_path' => '/tmp/notifier.log',
]);
deliveryCheck($targetHeavyConfig->notificationTargets === $manyNotificationTargets,
    'More than 32 notification targets must be accepted when error recipients fit');

/** @return array{code:int,stdout:string,stderr:string} */
function runEntrypoint(string $command, string $input, array $environment): array
{
    $process = proc_open($command, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, null, $environment);
    deliveryCheck(is_resource($process), 'CLI process must start');
    fwrite($pipes[0], $input); fclose($pipes[0]);
    $stdout = stream_get_contents($pipes[1]); fclose($pipes[1]);
    $stderr = stream_get_contents($pipes[2]); fclose($pipes[2]);
    return ['code' => proc_close($process), 'stdout' => (string) $stdout, 'stderr' => (string) $stderr];
}

function entrypointFrame(string $configJson, string $message = ''): string
{
    return StdinFrame::MAGIC . pack('NN', 0, strlen($configJson)) . $configJson . $message;
}

/** @return array{status:int,body:string,headers:array<string,string>} */
function response(int $status, string $description, array $headers = []): array
{
    return [
        'status' => $status,
        'body' => json_encode(['code' => $status, 'description' => $description], JSON_THROW_ON_ERROR),
        'headers' => $headers,
    ];
}

final class DeliverySendmailAdapter implements SendmailProcessAdapter
{
    /** @var list<string> */
    public array $messages = [];
    public function start(array $argv): SendmailProcessHandle
    {
        return new class($this) implements SendmailProcessHandle {
            private string $bytes = '';
            private bool $closed = false;
            public function __construct(private readonly DeliverySendmailAdapter $owner) {}
            public function writeStdin(string $bytes): int { $this->bytes .= $bytes; return strlen($bytes); }
            public function closeStdin(): void { $this->closed = true; $this->owner->messages[] = $this->bytes; }
            public function readStdout(): string { return ''; }
            public function readStderr(): string { return ''; }
            public function status(): array { return $this->closed
                ? ['running' => false, 'exitCode' => 0]
                : ['running' => true, 'exitCode' => null]; }
            public function terminate(int $signal): void { $this->closed = true; }
            public function close(): int { return 0; }
        };
    }
}

$requests = [];
$http = static function (string $url, string $payload, int $connectTimeout, int $timeout) use (&$requests): array {
    $requests[] = compact('url', 'payload', 'connectTimeout', 'timeout');
    return response(200, 'success');
};
$client = new WebhookClient('https://webhook.worksmobile.com/message/test-placeholder', $http, 256);
$result = $client->send('受信メール', '本文');
deliveryCheck($result->isSuccess(), 'Documented HTTP 200 response must succeed');
deliveryCheck(count($requests) === 1, 'Successful full payload must be sent exactly once');
deliveryCheck($requests[0]['connectTimeout'] === 5 && $requests[0]['timeout'] === 15, 'HTTP timeouts must be fixed at 5s/15s');
$decoded = json_decode($requests[0]['payload'], true, 512, JSON_THROW_ON_ERROR);
deliveryCheck($decoded === ['title' => '受信メール', 'body' => ['text' => '本文']], 'Webhook JSON schema must match the documented schema');

$liveResponse = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => [
        'status' => 200,
        'body' => '{"code":200,"description":"success "}',
        'headers' => [],
    ],
);
$liveResult = $liveResponse->send('Title', 'Text');
deliveryCheck($liveResult->isSuccess(), 'Observed HTTP 200 response with padded success description must succeed');
deliveryCheck($liveResult->classification === 'success', 'Observed padded success response must retain success classification');

$stringCodeResponse = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => [
        'status' => 200,
        'body' => '{"code":"200","description":"success"}',
        'headers' => [],
    ],
);
deliveryCheck(!$stringCodeResponse->send('Title', 'Text')->isSuccess(), 'Undocumented string response code must not be accepted');

$attempts = [];
$sleeps = [];
$rateLimited = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function (string $url, string $payload) use (&$attempts): array {
        $attempts[] = $payload;
        return count($attempts) === 1
            ? response(429, 'too many request', ['RateLimit-Reset' => '2'])
            : response(200, 'success');
    },
    256,
    static function (int $seconds) use (&$sleeps): void { $sleeps[] = $seconds; },
);
deliveryCheck($rateLimited->send('Title', 'Text')->isSuccess(), 'One bounded 429 retry may succeed');
deliveryCheck(count($attempts) === 2 && $attempts[0] === $attempts[1], '429 retry must reuse the identical payload once');
deliveryCheck($sleeps === [2], '429 retry must honor a bounded RateLimit-Reset');

foreach ([
    'missing parameter ' => 'missing_parameter',
    'invalid webhook URL ' => 'invalid_webhook_url',
] as $description => $classification) {
    $calls = 0;
    $badRequest = new WebhookClient(
        'https://webhook.worksmobile.com/message/test-placeholder',
        static function () use (&$calls, $description): array { ++$calls; return response(400, $description); },
        80,
    );
    $badResult = $badRequest->send('Title', str_repeat('long paragraph ', 20));
    deliveryCheck(!$badResult->isSuccess(), $description . ' must fail');
    deliveryCheck($badResult->classification === $classification, $description . ' must be classified after trimming');
    deliveryCheck($calls === 1, $description . ' must never trigger chunk fallback');
}

$chunkPayloads = [];
$chunking = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function (string $url, string $payload) use (&$chunkPayloads): array {
        $chunkPayloads[] = $payload;
        return count($chunkPayloads) === 1 ? response(400, 'invalid parameter') : response(200, 'success');
    },
    95,
);
$longText = "First paragraph has useful content.\n\nSecond paragraph has more useful content.\n\nThird paragraph closes it.";
deliveryCheck($chunking->send('Title', $longText)->isSuccess(), 'Oversoft-cap invalid parameter may fall back to chunks');
deliveryCheck(count($chunkPayloads) > 2, 'Chunk fallback must follow the explicitly rejected full request');
$chunks = array_map(static fn (string $payload): array => json_decode($payload, true, 512, JSON_THROW_ON_ERROR), array_slice($chunkPayloads, 1));
foreach ($chunks as $index => $chunk) {
    deliveryCheck(str_starts_with($chunk['body']['text'], '(' . ($index + 1) . '/'), 'Chunks must have deterministic sequence markers');
}

$smallCalls = 0;
$smallInvalid = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function () use (&$smallCalls): array { ++$smallCalls; return response(400, 'invalid parameter'); },
    4096,
);
deliveryCheck(!$smallInvalid->send('Title', 'short')->isSuccess() && $smallCalls === 1, 'Invalid parameter under soft cap must not split');

$timeoutCalls = 0;
$timeout = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function () use (&$timeoutCalls): array { ++$timeoutCalls; throw new RuntimeException('transport timeout with secret-placeholder'); },
    40,
);
deliveryCheck(!$timeout->send('Title', str_repeat('x', 500))->isSuccess(), 'Transport timeout must fail safely');
deliveryCheck($timeoutCalls === 1, 'Ambiguous timeout must not retry or split');

$logPath = tempnam(sys_get_temp_dir(), 'delivery-log-');
if ($logPath === false) {
    throw new RuntimeException('Could not create test log');
}
$logger = new OperationalLogger($logPath);
$successfulReporter = new ErrorReporter(
    new WebhookClient('https://webhook.worksmobile.com/message/test', static fn (): array => response(200, 'success')),
    $logger,
);
$successfulReporter->report(new RuntimeException('sensitive-value-placeholder'), str_repeat('a', 64));

$failedReporter = new ErrorReporter(
    new WebhookClient('https://webhook.worksmobile.com/message/test', static fn (): array => response(500, 'server error')),
    $logger,
);
$failedReporter->report(new RuntimeException('token=secret-placeholder'), str_repeat('b', 64));

$logs = file_get_contents($logPath);
deliveryCheck($logs !== false && $logs !== '', 'Operational events must be logged');
deliveryCheck(!str_contains($logs, 'secret-placeholder') && !str_contains($logs, '/message/test'), 'Logs must never contain webhook URLs, exception messages, or secret values');
foreach (array_filter(explode("\n", (string) $logs)) as $line) {
    $event = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
    deliveryCheck(array_keys($event) === ['timestamp', 'outcome', 'message_id_hash', 'classification', 'http_status'], 'Logs must contain only the allowlisted fields');
}
unlink($logPath);

foreach ([0, -1, 31] as $invalidSoftCap) {
    try {
        NotifierConfig::fromArray([
            'webhook_url' => 'https://webhook.worksmobile.com/message/test',
            'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
            'log_path' => '/tmp/notifier.log',
            'soft_cap_bytes' => $invalidSoftCap,
        ]);
        throw new RuntimeException('Invalid soft cap was accepted');
    } catch (InvalidArgumentException) {
        // Expected: configuration validation must match WebhookClient construction.
    }
}

$outsideConfig = NotifierConfig::fromArray([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['backup@example.invalid', 'operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
    'log_path' => '/tmp/notifier.log',
    'dedup_path' => '/tmp/notifier-dedup.json',
    'soft_cap_bytes' => 32,
]);
deliveryCheck($outsideConfig->softCapBytes === 32, 'Minimum WebhookClient soft cap must be accepted');
deliveryCheck($outsideConfig->errorRecipients === ['backup@example.invalid', 'operator@example.invalid'], 'Multiple canonical error recipients must be preserved');
deliveryCheck($outsideConfig->dedupPath === '/tmp/notifier-dedup.json', 'Private absolute dedup path must be preserved');

$legacyConfigDirectory = sys_get_temp_dir() . '/legacy-notifier-' . bin2hex(random_bytes(4));
mkdir($legacyConfigDirectory, 0700);
$legacyConfig = NotifierConfig::fromArray([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
    'log_path' => $legacyConfigDirectory . '/notifier.log',
]);
deliveryCheck($legacyConfig->dedupPath === $legacyConfigDirectory . '/delivery-dedup.json',
    'Pre-feature config must derive dedup state beside its validated private log');
deliveryCheck(!str_contains(strtolower($legacyConfig->dedupPath), '/public_html/'),
    'Derived dedup state must remain outside public_html');
$legacyDeduplicator = new DeliveryDeduplicator($legacyConfig->dedupPath);
deliveryCheck($legacyDeduplicator->claim(hash('sha256', 'legacy-startup')),
    'Pre-feature config must start with the derived dedup store');
deliveryCheck((fileperms($legacyConfigDirectory) & 0777) === 0700
    && (fileperms($legacyConfig->dedupPath) & 0777) === 0600,
    'Derived dedup state must retain private parent and file modes');
try {
    NotifierConfig::fromArray([
        'webhook_url' => 'https://webhook.worksmobile.com/message/test',
        'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
        'log_path' => $legacyConfigDirectory . '/notifier.log',
        'dedup_path' => $legacyConfigDirectory . '/public_html/claims.json',
    ]);
    throw new RuntimeException('Invalid explicit dedup_path was accepted');
} catch (InvalidArgumentException) {
    // Explicit invalid state paths must fail rather than using the legacy default.
}

$dedupDirectory = sys_get_temp_dir() . '/delivery-dedup-' . bin2hex(random_bytes(8));
mkdir($dedupDirectory, 0700);
$dedupPath = $dedupDirectory . '/claims.json';
$deduplicator = new DeliveryDeduplicator($dedupPath);
$hash = hash('sha256', 'message-one');
$now = new DateTimeImmutable('2026-07-12T00:00:00+00:00');
file_put_contents($dedupPath, '{}'); chmod($dedupPath, 0600);
deliveryCheck($deduplicator->claim($hash, $now), 'First delivery claim must succeed');
deliveryCheck(!$deduplicator->claim($hash, $now->modify('+599 seconds')), 'Unexpired delivery claim must be rejected');
deliveryCheck($deduplicator->claim($hash, $now->modify('+600 seconds')), 'Expired delivery claim must succeed');
$leaseHash = hash('sha256', 'leased-message');
$lease = $deduplicator->reserve($leaseHash, $now);
deliveryCheck(is_string($lease), 'First reservation must return an opaque lease token');
deliveryCheck($deduplicator->reserve($leaseHash, $now->modify('+599 seconds')) === null, 'Concurrent reservation must be suppressed inside the lease');
$expiredLease = $deduplicator->reserve($leaseHash, $now->modify('+600 seconds'));
deliveryCheck(is_string($expiredLease) && $expiredLease !== $lease, 'Crashed reservation must become retryable after bounded lease expiry');
$deduplicator->release($leaseHash, $expiredLease);
deliveryCheck(is_string($deduplicator->reserve($leaseHash, $now->modify('+601 seconds'))), 'Released reservation must be immediately retryable');
$dedupState = json_decode((string) file_get_contents($dedupPath), true, 32, JSON_THROW_ON_ERROR);
deliveryCheck(array_reduce(array_keys($dedupState), static fn (bool $ok, string $key): bool => $ok && preg_match('/\A[a-f0-9]{64}\z/', $key) === 1, true), 'Dedup state must contain lowercase SHA-256 keys only');
deliveryCheck((fileperms($dedupPath) & 0777) === 0600, 'Dedup state mode must be 0600');
foreach (['not-a-hash', strtoupper($hash)] as $invalidHash) {
    try { $deduplicator->claim($invalidHash, $now); throw new RuntimeException('Invalid hash accepted'); }
    catch (InvalidArgumentException) { /* Expected. */ }
}

$emptyObjectDirectory = sys_get_temp_dir() . '/delivery-dedup-empty-' . bin2hex(random_bytes(8));
mkdir($emptyObjectDirectory, 0700);
$emptyObjectPath = $emptyObjectDirectory . '/claims.json';
$emptyObjectDeduplicator = new DeliveryDeduplicator($emptyObjectPath);
$onlyHash = hash('sha256', 'only-reservation');
$onlyToken = $emptyObjectDeduplicator->reserve($onlyHash, $now);
deliveryCheck(is_string($onlyToken), 'Only reservation must be created');
$emptyObjectDeduplicator->release($onlyHash, $onlyToken);
$emptyObjectRaw = file_get_contents($emptyObjectPath);
deliveryCheck($emptyObjectRaw === "{}\n"
    && json_decode($emptyObjectRaw, false, 32, JSON_THROW_ON_ERROR) instanceof stdClass,
    'Releasing the final reservation must persist the canonical empty JSON object');
deliveryCheck(is_string($emptyObjectDeduplicator->reserve($onlyHash, $now->modify('+1 second'))),
    'A canonical empty dedup object must remain readable by the next reservation');
file_put_contents($emptyObjectPath, "[]\n"); chmod($emptyObjectPath, 0600);
$manualListRejected = false;
try { $emptyObjectDeduplicator->reserve(hash('sha256', 'manual-list'), $now); }
catch (RuntimeException) { $manualListRejected = true; }
deliveryCheck($manualListRejected, 'A manually supplied JSON list must remain rejected');
unlink($emptyObjectPath);
unlink($emptyObjectDirectory . '/.delivery-dedup.lock');
rmdir($emptyObjectDirectory);

file_put_contents($dedupPath, '{malformed');
try { $deduplicator->claim($hash, $now); throw new RuntimeException('Malformed dedup state accepted'); }
catch (RuntimeException) { /* Expected. */ }
unlink($dedupPath);
$dedupTarget = $dedupDirectory . '/target.json';
file_put_contents($dedupTarget, '{}');
$dedupAlias = $dedupDirectory . '/alias.json';
if (symlink($dedupTarget, $dedupAlias)) {
    try { new DeliveryDeduplicator($dedupAlias); throw new RuntimeException('Symlink dedup path accepted'); }
    catch (InvalidArgumentException) { /* Expected. */ }
    unlink($dedupAlias);
}
foreach (['relative.json', $dedupDirectory . '/public_html/claims.json'] as $invalidPath) {
    try { new DeliveryDeduplicator($invalidPath); throw new RuntimeException('Unsafe dedup path accepted'); }
    catch (InvalidArgumentException) { /* Expected. */ }
}
unlink($dedupTarget);
unlink($dedupDirectory . '/.delivery-dedup.lock');
rmdir($dedupDirectory);
$linkedDirectory = sys_get_temp_dir() . '/delivery-dedup-linked-' . bin2hex(random_bytes(8));
mkdir($linkedDirectory, 0700);
$linkedState = $linkedDirectory . '/claims.json';
file_put_contents($linkedState, '{}'); chmod($linkedState, 0600);
$linkedAlias = $linkedDirectory . '/claims-hardlink.json';
if (link($linkedState, $linkedAlias)) {
    $hardLinkRejected = false;
    try { (new DeliveryDeduplicator($linkedState))->claim($hash, $now); }
    catch (RuntimeException) { $hardLinkRejected = true; }
    deliveryCheck($hardLinkRejected, 'Hard-linked state must be rejected by descriptor link-count validation');
    unlink($linkedAlias);
}
unlink($linkedState);
foreach (glob($linkedDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
rmdir($linkedDirectory);
$openDedupDirectory = sys_get_temp_dir() . '/delivery-dedup-open-' . bin2hex(random_bytes(8));
mkdir($openDedupDirectory, 0755); chmod($openDedupDirectory, 0755);
$openDirectoryRejected = false;
try {
    (new DeliveryDeduplicator($openDedupDirectory . '/claims.json'))->claim($hash, $now);
} catch (RuntimeException) { $openDirectoryRejected = true; }
deliveryCheck($openDirectoryRejected, 'Non-private dedup directory must be rejected');
foreach (glob($openDedupDirectory . '/*') ?: [] as $file) { unlink($file); }
rmdir($openDedupDirectory);
foreach ([
    [],
    'operator@example.invalid',
    ['operator@example.invalid', "bad@example.invalid\r\nBcc: injected@example.invalid"],
    ['not-an-email'],
] as $invalidRecipients) {
    try {
        NotifierConfig::fromArray([
            'webhook_url' => 'https://webhook.worksmobile.com/message/test',
            'error_recipients' => $invalidRecipients,
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
            'log_path' => '/tmp/notifier.log',
        ]);
        throw new RuntimeException('Invalid error recipients were accepted');
    } catch (InvalidArgumentException) {
        // Expected.
    }
}
try {
    NotifierConfig::fromArray([
        'webhook_url' => 'https://webhook.worksmobile.com/message/test',
        'error_recipient' => 'operator@example.invalid',
        'log_path' => '/tmp/notifier.log',
    ]);
    throw new RuntimeException('Removed singular error_recipient key was accepted');
} catch (InvalidArgumentException) {
    // Expected.
}
$testToken = str_repeat('a', 32);
$armedConfig = NotifierConfig::fromArray([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
    'log_path' => '/tmp/notifier.log',
    'dedup_path' => '/tmp/notifier-dedup.json',
    'test_force_webhook_failure_until' => '2099-01-01T00:00:00+00:00',
    'test_error_subject_token' => $testToken,
]);
deliveryCheck($armedConfig->testErrorSubjectToken === $testToken, 'Valid temporary error-test configuration must load');
deliveryCheck(
    NotifierConfig::defaultPath('/home/example/public_html/bin') === '/home/example/private/config.json',
    'Default configuration must live outside the bin parent/public_html tree',
);
foreach (['/home/example/public_html/config.json', '/home/example/PUBLIC_HTML/config.json'] as $publicPath) {
    try {
        NotifierConfig::assertPrivatePath($publicPath);
        throw new RuntimeException('Public configuration path was accepted');
    } catch (InvalidArgumentException) {
        // Expected.
    }
}

$appRequests = [];
$appWebhook = new WebhookClient(
    'https://webhook.worksmobile.com/message/test',
    static function (string $url, string $payload) use (&$appRequests): array {
        $appRequests[] = $payload;
        return response(200, 'success');
    },
);
$brokenLogger = new OperationalLogger('/definitely/missing/directory/notifier.log');
$appReporter = new ErrorReporter($appWebhook, $brokenLogger);
$application = new DeliveryApplication($appWebhook, $appReporter, $brokenLogger);
$application->deliver(file_get_contents(dirname(__DIR__) . '/fixtures/plain.eml') ?: '');
deliveryCheck(count($appRequests) === 2, 'Internal delivery errors after reporter construction must be reported');
$deliveredPayload = json_decode($appRequests[0], true, 512, JSON_THROW_ON_ERROR);
deliveryCheck($deliveredPayload['title'] === '送信者 <sender@example.invalid>：お問い合わせ', 'Delivery must pass the formatter title to the webhook client');

$appDedupDirectory = sys_get_temp_dir() . '/application-dedup-' . bin2hex(random_bytes(8));
mkdir($appDedupDirectory, 0700);
$appDedup = new DeliveryDeduplicator($appDedupDirectory . '/claims.json');
$dedupRequests = [];
$dedupWebhook = new WebhookClient('https://webhook.worksmobile.com/message/test', static function () use (&$dedupRequests): array {
    $dedupRequests[] = true; return response(200, 'success');
});
$dedupLog = $appDedupDirectory . '/delivery.log';
$dedupLogger = new OperationalLogger($dedupLog);
$dedupReporter = new ErrorReporter($dedupWebhook, $dedupLogger);
$dedupApplication = new DeliveryApplication($dedupWebhook, $dedupReporter, $dedupLogger, null, $appDedup);
$sameRaw = "From: sender@example.invalid\r\nTo: target@example.invalid\r\nMessage-ID: <same@example.invalid>\r\n\r\nBody";
$otherRaw = str_replace('<same@example.invalid>', '<other@example.invalid>', $sameRaw);
$dedupApplication->deliver($sameRaw);
$dedupApplication->deliver($sameRaw);
$dedupApplication->deliver($otherRaw);
deliveryCheck(count($dedupRequests) === 2, 'Same Message-ID must deliver once while a different Message-ID delivers separately');
$withoutIdOne = "From: sender@example.invalid\r\nTo: target@example.invalid\r\nSubject: First\r\n\r\nBody";
$withoutIdTwo = str_replace('Subject: First', 'Subject: Second', $withoutIdOne);
$dedupApplication->deliver($withoutIdOne);
$dedupApplication->deliver($withoutIdOne);
$dedupApplication->deliver($withoutIdTwo);
deliveryCheck(count($dedupRequests) === 4, 'Raw RFC5322 fallback must deduplicate byte-identical messages without merging distinct messages');
$attachmentOne = "From: sender@example.invalid\r\nTo: target@example.invalid\r\nSubject: Same\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\nBody\r\n--x\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=same.bin\r\nContent-Transfer-Encoding: base64\r\n\r\nQUFBQQ==\r\n--x--\r\n";
$attachmentTwo = str_replace('QUFBQQ==', 'QkJCQg==', $attachmentOne);
$dedupApplication->deliver($attachmentOne);
$dedupApplication->deliver($attachmentTwo);
deliveryCheck(count($dedupRequests) === 6, 'Message-ID-less key must distinguish attachment bytes even when parsed metadata and sizes collide');
$lineEndingVariant = str_replace("\r\n", "\n", $withoutIdOne);
$dedupApplication->deliver($lineEndingVariant);
deliveryCheck(count($dedupRequests) === 7, 'Raw RFC5322 fallback must treat transport rewrites as distinct input');

$retryCalls = 0;
$retryWebhook = new WebhookClient('https://webhook.worksmobile.com/message/test', static function () use (&$retryCalls): array {
    ++$retryCalls;
    return $retryCalls === 1 ? response(500, 'server error') : response(200, 'success');
});
$retryReporter = new ErrorReporter($retryWebhook, $dedupLogger);
$retryApplication = new DeliveryApplication($retryWebhook, $retryReporter, $dedupLogger, null, $appDedup);
$retryRaw = str_replace('<same@example.invalid>', '<retry@example.invalid>', $sameRaw);
$retryApplication->deliver($retryRaw);
$retryApplication->deliver($retryRaw);
deliveryCheck($retryCalls === 3, 'Failed webhook delivery must release its reservation so the message can be retried');

$throwingReporter = new ErrorReporter(
    new WebhookClient('https://webhook.worksmobile.com/message/test', static fn (): array => response(500, 'server error')),
    $dedupLogger,
);
$reporterDeliveryCalls = 0;
$reporterDeliveryWebhook = new WebhookClient('https://webhook.worksmobile.com/message/test', static function () use (&$reporterDeliveryCalls): array {
    ++$reporterDeliveryCalls;
    return $reporterDeliveryCalls === 1 ? response(500, 'server error') : response(200, 'success');
});
$reporterRetryRaw = str_replace('<same@example.invalid>', '<reporter-retry@example.invalid>', $sameRaw);
$reporterRetryApplication = new DeliveryApplication($reporterDeliveryWebhook, $throwingReporter, $dedupLogger, null, $appDedup);
$reporterRetryApplication->deliver($reporterRetryRaw);
$reporterRetryApplication->deliver($reporterRetryRaw);
deliveryCheck($reporterDeliveryCalls === 2, 'Reporter failure must not prevent reservation release and later retry');

$failedStorePath = $appDedupDirectory . '/missing/claims.json';
$failedStore = new DeliveryDeduplicator($failedStorePath);
$failOpenRequests = [];
$failOpenWebhook = new WebhookClient('https://webhook.worksmobile.com/message/test', static function () use (&$failOpenRequests): array {
    $failOpenRequests[] = true; return response(200, 'success');
});
$failOpenLog = $appDedupDirectory . '/fail-open.log';
$failOpenLogger = new OperationalLogger($failOpenLog);
$failOpenReporter = new ErrorReporter($failOpenWebhook, $failOpenLogger);
(new DeliveryApplication($failOpenWebhook, $failOpenReporter, $failOpenLogger, null, $failedStore))->deliver($sameRaw);
deliveryCheck(count($failOpenRequests) === 1, 'Dedup store failure must fail open and deliver');
$failOpenContents = (string) file_get_contents($failOpenLog);
deliveryCheck(str_contains($failOpenContents, 'dedup_store_failure'), 'Dedup store failure must produce a safe operational event');
deliveryCheck(!str_contains($failOpenContents, $failedStorePath) && !str_contains($failOpenContents, '<same@'), 'Dedup failure log must omit paths and raw IDs');
foreach (glob($appDedupDirectory . '/*') ?: [] as $file) { if (is_file($file)) unlink($file); }
foreach (glob($appDedupDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
rmdir($appDedupDirectory);

$reportFailureCalls = 0;
$reportFailureWebhook = new WebhookClient(
    'https://webhook.worksmobile.com/message/test',
    static function () use (&$reportFailureCalls): array {
        ++$reportFailureCalls;
        return $reportFailureCalls === 1 ? response(500, 'server error') : response(429, 'too many request', ['RateLimit-Reset' => '1']);
    },
    32_768,
    static function (): void { throw new RuntimeException('reporter sleeper failed'); },
);
$reportFailureReporter = new ErrorReporter($reportFailureWebhook, $brokenLogger);
$reportFailureLog = tempnam(sys_get_temp_dir(), 'report-failure-log-');
deliveryCheck(is_string($reportFailureLog), 'Reporter failure test log must be created');
(new DeliveryApplication($reportFailureWebhook, $reportFailureReporter, new OperationalLogger($reportFailureLog)))
    ->deliver(file_get_contents(dirname(__DIR__) . '/fixtures/plain.eml') ?: '');
deliveryCheck($reportFailureCalls === 2, 'Reporter failure must be swallowed without retrying the reporter');
unlink($reportFailureLog);

$forcedRaw = "From: sender@example.invalid\r\nTo: target@example.invalid\r\nSubject: [Error Test {$testToken}]\r\nMessage-ID: <forced@example.invalid>\r\n\r\nSecret body";

$healthDirectory = sys_get_temp_dir() . '/delivery-health-integration-' . bin2hex(random_bytes(8));
mkdir($healthDirectory, 0700);
$healthDirectory = realpath($healthDirectory);
deliveryCheck(is_string($healthDirectory), 'Health integration directory must resolve');
$healthLog = $healthDirectory . '/operational.jsonl';
$healthAuth = new SystemMailAuthenticator(str_repeat('h', 32));
$healthSendmailAdapter = new DeliverySendmailAdapter();
$healthMonitor = new DeliveryHealthMonitor(
    $healthDirectory . '/delivery-health.json', ['operator@example.invalid'], $healthLog,
    $healthAuth,
    new SendmailClient($healthSendmailAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($healthLog),
    new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('1', 32), null,
        static fn (): array => ['home' => dirname($healthDirectory), 'uid' => posix_geteuid()],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('2', 32),
);
$observedCalls = 0;
$observedWebhook = new WebhookClient(
    'https://webhook.example.invalid/message/test',
    static function () use (&$observedCalls): array {
        ++$observedCalls;
        if ($observedCalls <= 2) {
            throw new RuntimeException('EXCEPTION_MARKER webhook.example.invalid HMAC_KEY_MARKER');
        }
        return response(200, 'success');
    },
    32_768,
    null,
    $healthMonitor,
);
$healthLogger = new OperationalLogger($healthLog);
$observedReporter = new ErrorReporter(
    $observedWebhook, $healthLogger, $healthMonitor,
);
$observedApplication = new DeliveryApplication(
    $observedWebhook, $observedReporter, $healthLogger, null, null, $healthAuth, $healthMonitor,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
);
$sensitiveRaw = "From: ORIGINAL_FROM_MARKER@example.invalid\r\n"
    . "To: ORIGINAL_TO_MARKER@example.invalid\r\n"
    . "Subject: ORIGINAL_SUBJECT_MARKER\r\n"
    . "Message-ID: <sensitive@example.invalid>\r\n\r\nORIGINAL_BODY_MARKER";
$observedApplication->deliver($sensitiveRaw);
deliveryCheck($observedCalls === 2 && $healthMonitor->status() === 'degraded'
    && count($healthSendmailAdapter->messages) === 1,
    'Normal failure plus error-webhook failure must produce one outage transition');
$degradedIntegration = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($degradedIntegration['next_observation_sequence'] === 2
    && $degradedIntegration['last_applied_sequence'] === 2,
    'Normal and error webhooks must reserve separate observations and apply only the error result');
foreach (['ORIGINAL_FROM_MARKER', 'ORIGINAL_TO_MARKER', 'ORIGINAL_SUBJECT_MARKER',
    'ORIGINAL_BODY_MARKER', 'EXCEPTION_MARKER', 'webhook.example.invalid', 'HMAC_KEY_MARKER'] as $privateMarker) {
    deliveryCheck(!str_contains($healthSendmailAdapter->messages[0], $privateMarker),
        'Outage email must omit original mail, exception, webhook, and key material');
}
$observedApplication->deliver($otherRaw);
deliveryCheck($observedCalls === 3 && $healthMonitor->status() === 'healthy'
    && count($healthSendmailAdapter->messages) === 2,
    'Next normal webhook success must produce one recovery transition');
$healthyIntegration = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($healthyIntegration['next_observation_sequence'] === 3
    && $healthyIntegration['last_applied_sequence'] === 3,
    'Logical success must reserve and apply exactly one observation');

$rateObservedCalls = 0;
$rateObservedClient = new WebhookClient(
    'https://webhook.example.invalid/message/test',
    static function () use (&$rateObservedCalls): array {
        ++$rateObservedCalls;
        return $rateObservedCalls === 1
            ? response(429, 'too many request', ['RateLimit-Reset' => '0'])
            : response(200, 'success');
    },
    256,
    static function (): void {},
    $healthMonitor,
);
$rateObservation = $rateObservedClient->sendObserved('Title', 'Text');
$rateState = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($rateObservation->sequence === 4 && $rateObservedCalls === 2
    && $rateState['next_observation_sequence'] === 4,
    'One logical 429 retry must reserve only one observation');

$chunkObservedCalls = 0;
$chunkObservedClient = new WebhookClient(
    'https://webhook.example.invalid/message/test',
    static function () use (&$chunkObservedCalls): array {
        ++$chunkObservedCalls;
        return $chunkObservedCalls === 1
            ? response(400, 'invalid parameter') : response(200, 'success');
    },
    95,
    null,
    $healthMonitor,
);
$chunkObservation = $chunkObservedClient->sendObserved('Title', $longText);
$chunkState = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($chunkObservation->sequence === 5 && $chunkObservedCalls > 2
    && $chunkState['next_observation_sequence'] === 5,
    'Full request and all chunk requests must share one logical observation');

$invalidObservedCalls = 0;
$invalidObservedClient = new WebhookClient(
    'https://webhook.example.invalid/message/test',
    static function () use (&$invalidObservedCalls): array { ++$invalidObservedCalls; return response(200, 'success'); },
    256,
    null,
    $healthMonitor,
);
$invalidObservation = $invalidObservedClient->sendObserved('Title', "\xff");
$invalidObservedState = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($invalidObservation->sequence === 6 && $invalidObservedCalls === 0
    && $invalidObservation->result->classification === 'invalid_payload'
    && $invalidObservedState['next_observation_sequence'] === 6,
    'Pre-HTTP payload failure must reserve one synthetic observation and perform no HTTP');

$forcedHealthDirectory = sys_get_temp_dir() . '/delivery-forced-health-' . bin2hex(random_bytes(8));
mkdir($forcedHealthDirectory, 0700);
$forcedHealthDirectory = realpath($forcedHealthDirectory);
deliveryCheck(is_string($forcedHealthDirectory), 'Forced health directory must resolve');
$forcedHealthLog = $forcedHealthDirectory . '/operational.jsonl';
$forcedHealthAdapter = new DeliverySendmailAdapter();
$forcedHealthMonitor = new DeliveryHealthMonitor(
    $forcedHealthDirectory . '/delivery-health.json', ['operator@example.invalid'], $forcedHealthLog,
    $healthAuth,
    new SendmailClient($forcedHealthAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($forcedHealthLog),
    new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('3', 32), null,
        static fn (): array => ['home' => dirname($forcedHealthDirectory), 'uid' => posix_geteuid()],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('4', 32),
);
$forcedObservedCalls = 0;
$forcedObservedWebhook = new WebhookClient(
    'https://webhook.example.invalid/message/test',
    static function () use (&$forcedObservedCalls): array { ++$forcedObservedCalls; return response(200, 'success'); },
    32_768, null, $forcedHealthMonitor,
);
$forcedObservedReporter = new ErrorReporter(
    $forcedObservedWebhook, new OperationalLogger($forcedHealthLog),
    $forcedHealthMonitor,
);
(new DeliveryApplication(
    $forcedObservedWebhook, $forcedObservedReporter, new OperationalLogger($forcedHealthLog),
    $armedConfig, null, $healthAuth, $forcedHealthMonitor,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
))->deliver($forcedRaw);
deliveryCheck($forcedObservedCalls === 0 && $forcedHealthMonitor->status() === 'degraded'
    && count($forcedHealthAdapter->messages) === 1,
    'Token-matched forced test must reserve one synthetic failure with zero HTTP');
$forcedState = json_decode((string) file_get_contents(
    $forcedHealthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($forcedState['next_observation_sequence'] === 1
    && $forcedState['last_applied_sequence'] === 1,
    'Forced test must reserve and apply exactly one synthetic observation');

$preflightDirectory = sys_get_temp_dir() . '/delivery-preflight-' . bin2hex(random_bytes(8));
mkdir($preflightDirectory, 0700);
$preflightDirectory = realpath($preflightDirectory);
deliveryCheck(is_string($preflightDirectory), 'Preflight directory must resolve');
$preflightLog = $preflightDirectory . '/operational.jsonl';
$preflightAdapter = new DeliverySendmailAdapter();
$preflightMonitor = new DeliveryHealthMonitor(
    $preflightDirectory . '/delivery-health.json', ['operator@example.invalid'], $preflightLog,
    $healthAuth,
    new SendmailClient($preflightAdapter, static fn (): float => 0.0, static fn (): bool => true),
    new OperationalLogger($preflightLog),
    new NativePrivateStateFilesystem(
        static fn (): string => str_repeat('5', 32), null,
        static fn (): array => ['home' => dirname($preflightDirectory), 'uid' => posix_geteuid()],
    ),
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static fn (): string => str_repeat('6', 32),
);
$preflightHttp = 0;
$preflightParser = 0;
$preflightWebhook = new WebhookClient(
    'https://webhook.example.invalid/message/test',
    static function () use (&$preflightHttp): array { ++$preflightHttp; return response(200, 'success'); },
    32_768, null, $preflightMonitor,
);
$preflightLogger = new OperationalLogger($preflightLog);
$preflightReporter = new ErrorReporter(
    $preflightWebhook, $preflightLogger, $preflightMonitor,
);
$preflightDedupPath = $preflightDirectory . '/delivery-dedup.json';
$preflightApplication = new DeliveryApplication(
    $preflightWebhook, $preflightReporter, $preflightLogger, null,
    new DeliveryDeduplicator($preflightDedupPath), $healthAuth, $preflightMonitor,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static function () use (&$preflightParser): never { ++$preflightParser; throw new RuntimeException('parser called'); },
);
$systemWire = $healthAuth->build(
    'error', ['operator@example.invalid'], 'Mon, 13 Jul 2026 12:00:00 +0000',
    str_repeat('7', 32), "UTC time: 2026-07-13T12:00:00Z\nClassification: transport_error\n"
        . 'Message-ID hash: ' . str_repeat('8', 64) . "\nOperational log: /private/example.invalid/operational.jsonl\n",
);
$preflightApplication->deliver($systemWire);
deliveryCheck($preflightParser === 0 && $preflightHttp === 0 && $preflightAdapter->messages === []
    && !file_exists($preflightDedupPath)
    && !file_exists($preflightDirectory . '/delivery-health.json'),
    'Authentic system mail must stop before parser, dedup, health, webhook, and sendmail');
$preflightEvents = array_values(array_filter(explode("\n", (string) file_get_contents($preflightLog))));
deliveryCheck(count($preflightEvents) === 1
    && json_decode($preflightEvents[0], true, 16, JSON_THROW_ON_ERROR)['classification'] === 'system_mail_suppressed',
    'Authentic system mail must emit only the allowlisted suppression log');

foreach ([
    'missing' => str_replace("X-Xserver-Mail-Notifier-Auth: ", "X-Removed-Auth: ", $systemWire),
    'duplicate' => str_replace("Subject: Xserver", "Subject: Xserver\r\nSubject: Xserver", $systemWire),
    'body-mutated' => str_replace('transport_error', 'transport_error_changed', $systemWire),
    'replay-mutated' => $healthAuth->build('recovery', ['operator@example.invalid'],
        'Mon, 13 Jul 2026 12:00:00 +0000', str_repeat('9', 32), "different body\n") . 'changed',
] as $forgeryName => $forgedWire) {
    $callsBefore = $preflightHttp;
    $ordinaryApplication = new DeliveryApplication(
        $preflightWebhook, $preflightReporter, $preflightLogger, null, null,
        $healthAuth, $preflightMonitor,
        static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    );
    $ordinaryApplication->deliver($forgedWire);
    deliveryCheck($preflightHttp > $callsBefore, $forgeryName . ' system headers must follow ordinary delivery');
}

foreach ([$healthDirectory, $forcedHealthDirectory, $preflightDirectory] as $cleanupDirectory) {
    foreach (glob($cleanupDirectory . '/*') ?: [] as $file) { if (is_file($file)) unlink($file); }
    foreach (glob($cleanupDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
    rmdir($cleanupDirectory);
}

$command = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(dirname(__DIR__, 2) . '/bin/mail-to-lineworks.php');
$descriptor = [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
$checkProcess = proc_open($command . ' --check-config', $descriptor, $checkPipes, null, ['MAIL_NOTIFIER_CONFIG' => '/definitely/missing/config.php']);
deliveryCheck(is_resource($checkProcess), 'Config check process must start');
fclose($checkPipes[0]); stream_get_contents($checkPipes[1]); fclose($checkPipes[1]); stream_get_contents($checkPipes[2]); fclose($checkPipes[2]);
deliveryCheck(proc_close($checkProcess) !== 0, 'Explicit config/startup test mode must return non-zero on failure');

$configDirectory = sys_get_temp_dir() . '/notifier-private-' . bin2hex(random_bytes(4));
mkdir($configDirectory);
$invalidConfigPath = $configDirectory . '/config.json';
file_put_contents($invalidConfigPath, json_encode([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
    'log_path' => $configDirectory . '/notifier.log',
    'soft_cap_bytes' => 0,
], JSON_THROW_ON_ERROR));
$invalidCapProcess = proc_open($command . ' --check-config', $descriptor, $invalidCapPipes, null, ['MAIL_NOTIFIER_CONFIG' => $invalidConfigPath]);
deliveryCheck(is_resource($invalidCapProcess), 'Invalid soft-cap config check must start');
fclose($invalidCapPipes[0]); stream_get_contents($invalidCapPipes[1]); fclose($invalidCapPipes[1]); stream_get_contents($invalidCapPipes[2]); fclose($invalidCapPipes[2]);
deliveryCheck(proc_close($invalidCapProcess) !== 0, 'Config check must reject a soft cap below WebhookClient minimum');

file_put_contents($invalidConfigPath, json_encode([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [], 'notification_targets' => [],
            'system_mail_hmac_key' => $configKey,
    'log_path' => $configDirectory . '/notifier.log',
    'dedup_path' => $configDirectory . '/dedup.json',
    'soft_cap_bytes' => 32,
], JSON_THROW_ON_ERROR));
$validCapProcess = proc_open($command . ' --check-config', $descriptor, $validCapPipes, null, ['MAIL_NOTIFIER_CONFIG' => $invalidConfigPath]);
deliveryCheck(is_resource($validCapProcess), 'Minimum valid soft-cap config check must start');
fclose($validCapPipes[0]); stream_get_contents($validCapPipes[1]); fclose($validCapPipes[1]); stream_get_contents($validCapPipes[2]); fclose($validCapPipes[2]);
deliveryCheck(proc_close($validCapProcess) === 0, 'Config check must fully construct WebhookClient at the minimum soft cap');

$frameConfig = (string) file_get_contents($invalidConfigPath);
$frameEnvironment = ['MAIL_NOTIFIER_STDIN_FRAME' => '1', 'MAIL_NOTIFIER_CONFIG' => '/definitely/missing/legacy.json'];
$framedConfigCheck = runEntrypoint($command . ' --check-config', entrypointFrame($frameConfig), $frameEnvironment);
deliveryCheck($framedConfigCheck['code'] === 0, 'Framed config check must work with no descriptor above stderr');
deliveryCheck($framedConfigCheck['stdout'] === '' && $framedConfigCheck['stderr'] === '', 'Framed config check must stay silent');

$framedMessage = "From: sender@example.invalid\r\nTo: receiver@example.invalid\r\nDate: Sat, 01 Jan 2000 00:00:00 +0900\r\nMessage-ID: <framed-dry-run@example.invalid>\r\nSubject: framed dry run\r\n\r\nbody\0bytes\r\n";
$framedMessageCheck = runEntrypoint($command . ' --check-message', entrypointFrame($frameConfig, $framedMessage), $frameEnvironment);
deliveryCheck($framedMessageCheck['code'] === 0, 'Framed message check must preserve and parse stdin message bytes');

$secretConfig = str_replace('/message/test', '/message/secret-frame-token', $frameConfig);
$invalidFrameConfig = json_decode($secretConfig, true, 32, JSON_THROW_ON_ERROR);
$invalidFrameConfig['soft_cap_bytes'] = 0;
$invalidFrameConfig = json_encode($invalidFrameConfig, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
foreach ([
    substr(entrypointFrame($secretConfig), 0, -1),
    entrypointFrame($invalidFrameConfig),
] as $startupFailureFrame) {
    $startupFailure = runEntrypoint($command, $startupFailureFrame, $frameEnvironment);
    deliveryCheck($startupFailure['code'] !== 0,
        'No-argument framed startup failures before reporter initialization must be nonzero');
    deliveryCheck($startupFailure['stdout'] === '' && $startupFailure['stderr'] === '',
        'No-argument framed startup failures must stay silent');
    deliveryCheck(!str_contains($startupFailure['stdout'] . $startupFailure['stderr'], 'secret-frame-token'),
        'No-argument framed startup failures must not expose configuration secrets');
}

$process = proc_open($command, $descriptor, $pipes, null, ['MAIL_NOTIFIER_CONFIG' => '/definitely/missing/config.php']);
deliveryCheck(is_resource($process), 'CLI process must start');
fwrite($pipes[0], "invalid mail input\n");
fclose($pipes[0]);
$stdout = stream_get_contents($pipes[1]); fclose($pipes[1]);
$stderr = stream_get_contents($pipes[2]); fclose($pipes[2]);
deliveryCheck(proc_close($process) !== 0, 'Delivery CLI startup failure before reporter initialization must be visible');
deliveryCheck($stdout === '' && $stderr === '', 'Delivery CLI startup failure must not leak exception details');

$offlineCommand = escapeshellarg(PHP_BINARY)
    . ' -d ' . escapeshellarg('disable_functions=curl_init,proc_open')
    . ' ' . escapeshellarg(dirname(__DIR__, 2) . '/bin/mail-to-lineworks.php');
$initializedFailure = runEntrypoint(
    $offlineCommand,
    "invalid mail after reporter initialization\n",
    ['MAIL_NOTIFIER_CONFIG' => $invalidConfigPath],
);
deliveryCheck($initializedFailure['code'] === 0,
    'Ordinary delivery failure after reporter initialization must remain exit zero');
deliveryCheck($initializedFailure['stdout'] === '' && $initializedFailure['stderr'] === '',
    'Initialized delivery failure must stay silent');

foreach (['', substr(StdinFrame::MAGIC, 0, -1), entrypointFrame('[]'), entrypointFrame($secretConfig, '',)] as $index => $badFrame) {
    if ($index === 3) $badFrame = substr($badFrame, 0, strlen(StdinFrame::MAGIC) + 8 + strlen($secretConfig) - 1);
    foreach (['--check-config', '--check-message'] as $checkArgument) {
        $failedFrame = runEntrypoint($command . ' ' . $checkArgument, $badFrame, $frameEnvironment);
        deliveryCheck($failedFrame['code'] !== 0, 'Malformed or non-object flagged frame must fail closed in check modes');
        deliveryCheck(!str_contains($failedFrame['stdout'] . $failedFrame['stderr'], 'secret-frame-token'), 'Framed failure must not expose config secrets');
    }
}

$legacyMagicMessage = StdinFrame::MAGIC . "\r\nFrom: sender@example.invalid\r\nTo: receiver@example.invalid\r\nSubject: legacy magic\r\n\r\nbody\r\n";
$legacyMagicCheck = runEntrypoint($command . ' --check-message', $legacyMagicMessage, ['MAIL_NOTIFIER_CONFIG' => $invalidConfigPath]);
deliveryCheck($legacyMagicCheck['code'] === 0, 'Unflagged magic-prefixed mail must remain ordinary legacy stdin');
$nonExactFlagCheck = runEntrypoint($command . ' --check-message', $legacyMagicMessage,
    ['MAIL_NOTIFIER_STDIN_FRAME' => '01', 'MAIL_NOTIFIER_CONFIG' => $invalidConfigPath]);
deliveryCheck($nonExactFlagCheck['code'] === 0, 'Only the exact frame flag value 1 may enable decoding');

foreach (['--unknown', '--check-config --check-message'] as $arguments) {
    $badArguments = runEntrypoint($command . ' ' . $arguments, entrypointFrame($frameConfig, $framedMessage), $frameEnvironment);
    deliveryCheck($badArguments['code'] !== 0, 'Unknown or simultaneous check arguments must be rejected');
    deliveryCheck($badArguments['stdout'] === '' && $badArguments['stderr'] === '', 'Argument rejection must stay silent');
}

$messageCheckProcess = proc_open($command . ' --check-message', $descriptor, $messageCheckPipes, null,
    ['MAIL_NOTIFIER_CONFIG' => $invalidConfigPath]);
deliveryCheck(is_resource($messageCheckProcess), 'RFC822 parser dry-run must start');
fwrite($messageCheckPipes[0], "From: sender@example.invalid\r\nTo: receiver@example.invalid\r\nDate: Sat, 01 Jan 2000 00:00:00 +0900\r\nMessage-ID: <dry-run@example.invalid>\r\nSubject: dry run\r\n\r\nbody\r\n");
fclose($messageCheckPipes[0]);
$messageCheckStdout = stream_get_contents($messageCheckPipes[1]); fclose($messageCheckPipes[1]);
$messageCheckStderr = stream_get_contents($messageCheckPipes[2]); fclose($messageCheckPipes[2]);
deliveryCheck(proc_close($messageCheckProcess) === 0, 'RFC822 parser dry-run must exercise the real parser');
deliveryCheck($messageCheckStdout === '' && $messageCheckStderr === '', 'RFC822 parser dry-run must stay silent');

$publicDirectory = $configDirectory . '/public_html';
mkdir($publicDirectory);
$publicConfigPath = $publicDirectory . '/config.json';
copy($invalidConfigPath, $publicConfigPath);
$publicProcess = proc_open($command . ' --check-config', $descriptor, $publicPipes, null, ['MAIL_NOTIFIER_CONFIG' => $publicConfigPath]);
deliveryCheck(is_resource($publicProcess), 'Public-path config check must start');
fclose($publicPipes[0]); stream_get_contents($publicPipes[1]); fclose($publicPipes[1]); stream_get_contents($publicPipes[2]); fclose($publicPipes[2]);
deliveryCheck(proc_close($publicProcess) !== 0, 'Config override in public_html must be rejected');
$aliasPath = $configDirectory . '/config-alias.json';
if (symlink($publicConfigPath, $aliasPath)) {
    try {
        NotifierConfig::assertPrivatePath($aliasPath);
        throw new RuntimeException('Symlink into public_html was accepted');
    } catch (InvalidArgumentException) {
        // Expected: realpath boundary must be enforced as well as lexical boundary.
    }
    unlink($aliasPath);
}
$directoryAlias = $configDirectory . '/private-alias';
if (symlink($publicDirectory, $directoryAlias)) {
    try {
        NotifierConfig::assertPrivatePath($directoryAlias . '/not-yet-created.json');
        throw new RuntimeException('Nonexistent config below public_html symlink was accepted');
    } catch (InvalidArgumentException) {
        // Expected: resolve the deepest existing ancestor even before config creation.
    }
    unlink($directoryAlias);
}
unlink($publicConfigPath);
unlink($invalidConfigPath);
if (is_file($configDirectory . '/notifier.log')) unlink($configDirectory . '/notifier.log');
rmdir($publicDirectory);
rmdir($configDirectory);

fwrite(STDOUT, "PASS: delivery and fallback\n");
