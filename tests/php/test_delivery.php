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
use XserverMail\WebhookAttemptDiagnostic;
use XserverMail\WebhookDiagnostic;
use XserverMail\StdinFrame;

function deliveryCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

/** @return array{headers:array<string,string>,subject:string,body:string} */
function decodeDeliverySystemWire(string $wire): array
{
    $boundary = strpos($wire, "\r\n\r\n");
    deliveryCheck($boundary !== false, 'System wire must contain a CRLF header boundary');
    $headers = [];
    foreach (explode("\r\n", substr($wire, 0, $boundary)) as $line) {
        $separator = strpos($line, ': ');
        deliveryCheck($separator !== false, 'System wire headers must use canonical syntax');
        $headers[strtolower(substr($line, 0, $separator))] = substr($line, $separator + 2);
    }
    $subject = '';
    deliveryCheck(preg_match_all('/=\?UTF-8\?B\?([^?]+)\?=/D', $headers['subject'] ?? '', $matches) > 0,
        'System subject must use RFC 2047 UTF-8 Base64 encoded words');
    foreach ($matches[1] as $chunk) {
        $decoded = base64_decode($chunk, true);
        deliveryCheck(is_string($decoded), 'System subject must strict-decode');
        $subject .= $decoded;
    }
    $body = base64_decode(str_replace("\r\n", '', substr($wire, $boundary + 4)), true);
    deliveryCheck(is_string($body), 'System body must strict-decode');
    return ['headers' => $headers, 'subject' => $subject, 'body' => $body];
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

$serverAttempts = [];
$serverSleeps = [];
$serverFailure = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function (string $url, string $payload) use (&$serverAttempts): array {
        $serverAttempts[] = $payload;
        return count($serverAttempts) === 1
            ? response(500, 'server error')
            : response(200, 'success');
    },
    256,
    static function (int $seconds) use (&$serverSleeps): void { $serverSleeps[] = $seconds; },
);
deliveryCheck($serverFailure->send('Title', 'Text')->isSuccess(), 'One bounded HTTP 5xx retry may succeed');
deliveryCheck(
    count($serverAttempts) === 2 && $serverAttempts[0] === $serverAttempts[1],
    'HTTP 5xx retry must reuse the identical payload once',
);
deliveryCheck($serverSleeps === [5], 'HTTP 5xx retry must wait before retrying');

foreach ([
    ['429-then-500', [
        response(429, 'too many request', ['RateLimit-Reset' => '1']),
        response(500, 'server error'),
    ], [1], [429, 500]],
    ['500-then-429', [
        response(500, 'server error'),
        response(429, 'too many request', ['RateLimit-Reset' => '1']),
    ], [5], [500, 429]],
] as [$label, $responses, $expectedSleeps, $expectedStatuses]) {
    $crossCalls = 0;
    $crossSleeps = [];
    $crossClient = new WebhookClient(
        'https://webhook.worksmobile.com/message/test-placeholder',
        static function () use (&$crossCalls, &$responses): array {
            ++$crossCalls;
            return array_shift($responses);
        },
        256,
        static function (int $seconds) use (&$crossSleeps): void { $crossSleeps[] = $seconds; },
    );
    $crossResult = $crossClient->send('Title', 'Text');
    deliveryCheck(!$crossResult->isSuccess() && $crossCalls === 2,
        $label . ' must stop after two identical-payload sends');
    deliveryCheck($crossSleeps === $expectedSleeps,
        $label . ' must sleep only for the initial retry transition');
    deliveryCheck($crossResult->diagnostic?->attemptHttpStatuses() === $expectedStatuses,
        $label . ' must retain exactly two state-machine attempts');
}

foreach ([null, '-1', '16', '1.5'] as $invalidReset) {
    $invalidResetCalls = 0;
    $headers = $invalidReset === null ? [] : ['RateLimit-Reset' => $invalidReset];
    $invalidResetClient = new WebhookClient(
        'https://webhook.worksmobile.com/message/test-placeholder',
        static function () use (&$invalidResetCalls, $headers): array {
            ++$invalidResetCalls;
            return response(429, 'too many request', $headers);
        },
        256,
        static function (): void { throw new RuntimeException('Invalid reset must not sleep'); },
    );
    deliveryCheck(!$invalidResetClient->send('Title', 'Text')->isSuccess()
        && $invalidResetCalls === 1, 'Invalid RateLimit-Reset must not transition to retry');
}

$echoedDescription = 'private payload fragment';
$echoResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => response(400, $echoedDescription),
))->send('Title', 'Prefix ' . $echoedDescription . ' suffix');
deliveryCheck($echoResult->diagnostic?->attempts[0]->providerDescription === null,
    'Provider description that directly echoes an 8+ character payload fragment must not be retained');

$wrappedEchoResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => response(400, 'provider rejected: private payload fragment (invalid)'),
))->send('Title', 'Prefix private payload fragment suffix');
deliveryCheck($wrappedEchoResult->diagnostic?->attempts[0]->providerDescription === null,
    'Provider description with a prefix and suffix around an 8-byte payload fragment must be redacted');

$japaneseEchoResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => response(400, 'エラー: 秘密情報 が含まれます'),
))->send('件名に秘密情報があります', '本文');
deliveryCheck($japaneseEchoResult->diagnostic?->attempts[0]->providerDescription === null,
    'Provider description sharing eight payload bytes inside Japanese text must be redacted');

$largeEchoText = str_repeat('x', 10 * 1024 * 1024) . 'private-tail-fragment';
$largeEchoStarted = microtime(true);
$largeEchoResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => response(400, 'provider prefix private-tail-fragment provider suffix'),
))->send('Title', $largeEchoText);
$largeEchoElapsed = microtime(true) - $largeEchoStarted;
deliveryCheck($largeEchoResult->diagnostic?->attempts[0]->providerDescription === null,
    'Provider description sharing an internal fragment with a 10 MiB payload must be redacted');
deliveryCheck($largeEchoElapsed < 5.0,
    'Payload echo detection must scan a 10 MiB payload without repeated pathological full-string searches');

$unrelatedDescription = 'ordinary provider explanation';
$unrelatedEchoResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => response(400, $unrelatedDescription),
))->send('Unrelated title', 'Completely separate webhook body');
deliveryCheck($unrelatedEchoResult->diagnostic?->attempts[0]->providerDescription === $unrelatedDescription,
    'Unrelated provider descriptions must remain diagnostically useful');

$shortEchoResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => response(400, 'seven77'),
))->send('Title', 'Prefix seven77 suffix');
deliveryCheck($shortEchoResult->diagnostic?->attempts[0]->providerDescription === 'seven77',
    'Provider description shorter than eight characters must remain diagnostically useful');

$diagnosticResponses = [
    [
        'status' => 500,
        'body' => "{\"code\":\"E500\",\"description\":\"temporary\\u0000 failure\"}",
        'headers' => ['Content-Type' => 'application/json; charset=UTF-8'],
    ],
    response(200, 'success'),
];
$diagnosticClient = new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function () use (&$diagnosticResponses): array {
        return array_shift($diagnosticResponses);
    },
    256,
    static function (): void {},
);
$diagnosticResult = $diagnosticClient->send('題名', '本文');
deliveryCheck($diagnosticResult->isSuccess(), 'HTTP 5xx retry must recover');
deliveryCheck($diagnosticResult->diagnostic instanceof WebhookDiagnostic, 'Webhook result must expose diagnostics');
deliveryCheck($diagnosticResult->diagnostic->attemptHttpStatuses() === [500, 200], 'Diagnostics must preserve both statuses');
deliveryCheck($diagnosticResult->diagnostic->recoveredByRetry, 'Retry recovery must be explicit');
deliveryCheck($diagnosticResult->diagnostic->attempts[0]->providerCode === 'E500', 'Provider code must be preserved safely');
deliveryCheck($diagnosticResult->diagnostic->attempts[0]->providerDescription === 'temporary failure', 'Controls must be removed');

foreach ([
    ['provider code at 64', str_repeat('c', 64), 'description', 'text/plain', str_repeat('c', 64), 'description', 'text/plain'],
    ['provider code at 65', str_repeat('c', 65), 'description', 'text/plain', str_repeat('c', 64), 'description', 'text/plain'],
    ['provider description at 200', 'code', str_repeat('d', 200), 'text/plain', 'code', str_repeat('d', 200), 'text/plain'],
    ['provider description at 201', 'code', str_repeat('d', 201), 'text/plain', 'code', str_repeat('d', 200), 'text/plain'],
    ['content type at 100', 'code', 'description', str_repeat('t', 100), 'code', 'description', str_repeat('t', 100)],
    ['content type at 101', 'code', 'description', str_repeat('t', 101), 'code', 'description', str_repeat('t', 100)],
    ['C1 controls', "co\u{0085}de", "desc\u{0085}ription", "text/\u{0085}plain", 'code', 'description', 'text/plain'],
] as [$limitCase, $code, $description, $contentType, $expectedCode, $expectedDescription, $expectedContentType]) {
    $limitResult = (new WebhookClient(
        'https://webhook.worksmobile.com/message/test-placeholder',
        static fn (): array => [
            'status' => 400,
            'body' => json_encode(['code' => $code, 'description' => $description], JSON_THROW_ON_ERROR),
            'headers' => ['Content-Type' => $contentType],
        ],
    ))->send('Title', 'Text');
    $limitAttempt = $limitResult->diagnostic?->attempts[0] ?? null;
    deliveryCheck($limitAttempt?->providerCode === $expectedCode, $limitCase . ' must use its exact code boundary');
    deliveryCheck($limitAttempt?->providerDescription === $expectedDescription, $limitCase . ' must use its exact description boundary');
    deliveryCheck($limitAttempt?->responseContentType === $expectedContentType, $limitCase . ' must use its exact content type boundary');
}

$invalidJsonBody = 'untrusted response body placeholder';
$invalidJsonResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static fn (): array => ['status' => 502, 'body' => $invalidJsonBody, 'headers' => []],
))->send('Title', 'Text');
deliveryCheck($invalidJsonResult->diagnostic instanceof WebhookDiagnostic, 'Invalid JSON result must expose diagnostics');
$invalidJsonAttempt = $invalidJsonResult->diagnostic->attempts[0];
deliveryCheck($invalidJsonAttempt->responseFormat === 'invalid_json', 'Non-JSON response must be identified without retaining its body');
deliveryCheck(!array_key_exists('responseBody', get_object_vars($invalidJsonAttempt)), 'Diagnostics must not expose a response body');
deliveryCheck(!str_contains(serialize($invalidJsonAttempt), $invalidJsonBody), 'Diagnostics must not retain a response body');

$transportMessage = 'transport secret placeholder';
$transportResult = (new WebhookClient(
    'https://webhook.worksmobile.com/message/test-placeholder',
    static function () use ($transportMessage): array {
        throw new RuntimeException($transportMessage);
    },
))->send('Title', 'Text');
deliveryCheck($transportResult->diagnostic instanceof WebhookDiagnostic, 'Transport result must expose diagnostics');
$transportAttempt = $transportResult->diagnostic->attempts[0];
deliveryCheck($transportAttempt->responseFormat === 'transport_error', 'Transport exceptions must be identified without their message');
deliveryCheck(!str_contains(serialize($transportAttempt), $transportMessage), 'Diagnostics must not retain exception messages');

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

$logDirectory = sys_get_temp_dir() . '/delivery-log-' . bin2hex(random_bytes(8));
mkdir($logDirectory, 0700);
$logDirectory = realpath($logDirectory);
deliveryCheck(is_string($logDirectory), 'Operational log fixture directory must resolve');
$logPath = $logDirectory . '/operational.jsonl';
$logger = new OperationalLogger($logPath);
$echoLogPath = $logDirectory . '/provider-echo.jsonl';
(new OperationalLogger($echoLogPath))->log(
    'failure', str_repeat('9', 64), $echoResult->classification,
    $echoResult->httpStatus, $echoResult->diagnostic,
);
$echoLogBytes = (string) file_get_contents($echoLogPath);
deliveryCheck(!str_contains($echoLogBytes, $echoedDescription)
    && str_contains($echoLogBytes, '"provider_description":null'),
    'Direct provider echo of a payload fragment must not reach persistent diagnostics');
$logger->log('success', str_repeat('0', 64), 'success', 200);
$logger->log(
    'success', str_repeat('1', 64), 'success', 200, $diagnosticResult->diagnostic,
);

$applicationResponses = [
    [
        'status' => 500,
        'body' => '{"code":"E500","description":"temporary failure","raw":"RESPONSE_BODY_MARKER"}',
        'headers' => ['Content-Type' => 'application/json'],
    ],
    response(200, 'success'),
];
$applicationWebhook = new WebhookClient(
    'https://webhook.worksmobile.com/message/secret-placeholder',
    static function () use (&$applicationResponses): array { return array_shift($applicationResponses); },
    256,
    static function (): void {},
);
$applicationReporter = new ErrorReporter($applicationWebhook, $logger);
(new DeliveryApplication($applicationWebhook, $applicationReporter, $logger))->deliver(
    "From: ADDRESS_MARKER@example.invalid\r\n"
    . "To: receiver@example.invalid\r\n"
    . "Subject: SUBJECT_MARKER\r\n"
    . "Content-Type: multipart/mixed; boundary=x\r\n\r\n"
    . "--x\r\nContent-Type: text/plain\r\n\r\nMAIL_BODY_MARKER\r\n"
    . "--x\r\nContent-Disposition: attachment; filename=ATTACHMENT_MARKER.txt\r\n\r\nx\r\n--x--\r\n",
);

$reporter = new ErrorReporter(
    new WebhookClient(
        'https://webhook.worksmobile.com/message/test-placeholder',
        static fn (): array => [
            'status' => 400,
            'body' => '{"code":"E400","description":"invalid parameter","raw":"REPORTER_RESPONSE_BODY_MARKER"}',
            'headers' => ['Content-Type' => 'application/json'],
        ],
    ),
    $logger,
);
$reporter->report(new RuntimeException('EXCEPTION_MESSAGE_MARKER'), str_repeat('b', 64));

$logs = file_get_contents($logPath);
deliveryCheck($logs !== false && $logs !== '', 'Operational events must be logged');
$events = array_map(
    static fn (string $line): array => json_decode($line, true, 512, JSON_THROW_ON_ERROR),
    array_values(array_filter(explode("\n", (string) $logs))),
);
deliveryCheck(array_keys($events[0]) === [
    'timestamp', 'outcome', 'message_id_hash', 'classification', 'http_status',
], 'Diagnostic-free logs must preserve the legacy five-field schema');
$event = $events[1];
deliveryCheck(array_keys($event) === [
    'timestamp', 'outcome', 'message_id_hash', 'classification', 'http_status',
    'attempt_count', 'attempt_http_statuses', 'provider_code', 'provider_description',
    'response_format', 'response_content_type', 'response_body_bytes', 'response_body_sha256',
    'payload_bytes', 'title_characters', 'text_characters', 'recovered_by_retry',
], 'Diagnostic logs must contain exactly the allowlisted fields');
deliveryCheck(array_slice($event, 5, null, true) === [
    'attempt_count' => 2,
    'attempt_http_statuses' => [500, 200],
    'provider_code' => 'E500',
    'provider_description' => 'temporary failure',
    'response_format' => 'json',
    'response_content_type' => 'application/json; charset=UTF-8',
    'response_body_bytes' => 55,
    'response_body_sha256' => 'e75177c2c53517db46aa9c0e13571a1e9f5b28f867e398822e43201983c49ab3',
    'payload_bytes' => 43,
    'title_characters' => 2,
    'text_characters' => 2,
    'recovered_by_retry' => true,
], 'Diagnostic logs must preserve every approved value from the last failed attempt');
deliveryCheck(($events[2]['attempt_http_statuses'] ?? null) === [500, 200]
    && ($events[2]['provider_code'] ?? null) === 'E500',
    'DeliveryApplication must pass webhook diagnostics to the operational log');
deliveryCheck(($events[3]['attempt_http_statuses'] ?? null) === [400]
    && ($events[3]['provider_code'] ?? null) === 'E400',
    'ErrorReporter must pass error-webhook diagnostics to the operational log');
foreach ([
    'ADDRESS_MARKER@example.invalid', 'SUBJECT_MARKER', 'MAIL_BODY_MARKER', 'ATTACHMENT_MARKER.txt',
    'secret-placeholder', 'test-placeholder', 'EXCEPTION_MESSAGE_MARKER',
    'RESPONSE_BODY_MARKER', 'REPORTER_RESPONSE_BODY_MARKER',
    '{"code":"E500","description":"temporary failure"',
] as $secret) {
    deliveryCheck(!str_contains((string) $logs, $secret),
        'Operational diagnostics must omit mail, webhook, exception, and raw response secrets');
}
deliveryCheck((fileperms($logPath) & 0777) === 0600, 'New operational log must be mode 0600');

$loggerBoundaryPath = $logDirectory . '/diagnostic-boundaries.jsonl';
$loggerBoundary = new OperationalLogger($loggerBoundaryPath);
$loggerBoundaryCases = [
    ['code-64', str_repeat('c', 64), 'description', 'text/plain', str_repeat('c', 64), 'description', 'text/plain'],
    ['code-65', str_repeat('c', 65), 'description', 'text/plain', str_repeat('c', 64), 'description', 'text/plain'],
    ['description-200', 'code', str_repeat('d', 200), 'text/plain', 'code', str_repeat('d', 200), 'text/plain'],
    ['description-201', 'code', str_repeat('d', 201), 'text/plain', 'code', str_repeat('d', 200), 'text/plain'],
    ['content-type-100', 'code', 'description', str_repeat('t', 100), 'code', 'description', str_repeat('t', 100)],
    ['content-type-101', 'code', 'description', str_repeat('t', 101), 'code', 'description', str_repeat('t', 100)],
    ['C1-controls', "co\u{0085}de", "desc\u{0085}ription", "text/\u{0085}plain", 'code', 'description', 'text/plain'],
];
foreach ($loggerBoundaryCases as [$label, $code, $description, $contentType]) {
    $loggerBoundary->log('failure', str_repeat('6', 64), 'http_error', 400, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(
            400, $code, $description, 'json', $contentType, 2,
            'd4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35',
        )],
        10, 3, 4, false,
    ));
}
$loggerBoundaryEvents = array_map(
    static fn (string $line): array => json_decode($line, true, 16, JSON_THROW_ON_ERROR),
    array_values(array_filter(explode("\n", (string) file_get_contents($loggerBoundaryPath)))),
);
foreach ($loggerBoundaryCases as $index => [$label, , , , $expectedCode, $expectedDescription, $expectedContentType]) {
    deliveryCheck(($loggerBoundaryEvents[$index]['provider_code'] ?? null) === $expectedCode,
        $label . ' must be re-bounded by OperationalLogger');
    deliveryCheck(($loggerBoundaryEvents[$index]['provider_description'] ?? null) === $expectedDescription,
        $label . ' description must be re-bounded by OperationalLogger');
    deliveryCheck(($loggerBoundaryEvents[$index]['response_content_type'] ?? null) === $expectedContentType,
        $label . ' content type must be re-bounded by OperationalLogger');
}

$beforeInvalidFormat = (string) file_get_contents($loggerBoundaryPath);
$invalidFormatDiagnostic = new WebhookDiagnostic(
    [new WebhookAttemptDiagnostic(
        400, 'E400', 'invalid parameter', 'xml', 'application/xml', 20,
        'f5a45a5bb456a9ec2a298f9b7b22c08ec7e8c92a9b7c38b5116f8f396399020a',
    )],
    10, 3, 4, false,
);
try {
    $loggerBoundary->log('failure', str_repeat('7', 64), 'http_error', 400, $invalidFormatDiagnostic);
    throw new RuntimeException('Unapproved response format was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Unapproved response format was accepted',
        'OperationalLogger must reject response formats outside its allowlist');
}
deliveryCheck((string) file_get_contents($loggerBoundaryPath) === $beforeInvalidFormat,
    'Rejected response format must not append any part of an event');

$validBodyHash = hash('sha256', 'diagnostic response');
$strictLoggerCases = [
    ['unknown-outcome', 'other', 'http_error', 400, null],
    ['unknown-classification', 'failure', 'not_allowlisted', 400, null],
    ['status-below-http-range', 'failure', 'http_error', 99, null],
    ['success-outcome-mismatch', 'success', 'http_error', 500, null],
    ['ignored-outcome-mismatch', 'ignored', 'http_error', null, null],
    ['attempt-status-below-range', 'failure', 'http_error', 99, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(99, 'E99', 'server error', 'json', 'application/json', 2, $validBodyHash)],
        10, 3, 4, false,
    )],
    ['negative-response-bytes', 'failure', 'http_error', 500, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(500, 'E500', 'server error', 'json', 'application/json', -1, $validBodyHash)],
        10, 3, 4, false,
    )],
    ['negative-payload-bytes', 'failure', 'http_error', 500, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(500, 'E500', 'server error', 'json', 'application/json', 2, $validBodyHash)],
        -1, 3, 4, false,
    )],
    ['transport-with-metadata', 'failure', 'transport_error', null, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(null, 'secret', null, 'transport_error', null, 0, null)],
        10, 3, 4, false,
    )],
    ['invalid-json-with-provider-code', 'failure', 'http_error', 502, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(502, 'E502', null, 'invalid_json', 'text/plain', 2, $validBodyHash)],
        10, 3, 4, false,
    )],
    ['json-without-body-hash', 'failure', 'http_error', 500, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(500, 'E500', 'server error', 'json', 'application/json', 2, null)],
        10, 3, 4, false,
    )],
    ['recovered-without-retryable-transition', 'success', 'success', 200, new WebhookDiagnostic(
        [
            new WebhookAttemptDiagnostic(400, 'E400', 'invalid parameter', 'json', 'application/json', 2, $validBodyHash),
            new WebhookAttemptDiagnostic(200, 200, 'success', 'json', 'application/json', 2, $validBodyHash),
        ],
        10, 3, 4, true,
    )],
    ['failure-with-success-tuple', 'failure', 'success', 200, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(200, 200, 'success', 'json', 'application/json', 2, $validBodyHash)],
        10, 3, 4, false,
    )],
    ['event-status-does-not-match-last-attempt', 'failure', 'http_error', 500, new WebhookDiagnostic(
        [new WebhookAttemptDiagnostic(400, 'E400', 'server error', 'json', 'application/json', 2, $validBodyHash)],
        10, 3, 4, false,
    )],
    ['transport-before-later-http-attempt', 'failure', 'http_error', 500, new WebhookDiagnostic(
        [
            new WebhookAttemptDiagnostic(null, null, null, 'transport_error', null, 0, null),
            new WebhookAttemptDiagnostic(500, 'E500', 'server error', 'json', 'application/json', 2, $validBodyHash),
        ],
        10, 3, 4, false,
    )],
];
foreach ($strictLoggerCases as [$label, $outcome, $classification, $status, $diagnostic]) {
    $beforeRejectedDiagnostic = (string) file_get_contents($loggerBoundaryPath);
    try {
        $loggerBoundary->log(
            $outcome, str_repeat('8', 64), $classification, $status, $diagnostic,
        );
        throw new RuntimeException($label . ' was accepted');
    } catch (RuntimeException $exception) {
        deliveryCheck($exception->getMessage() !== $label . ' was accepted',
            $label . ' must be rejected by the operational logging boundary');
    }
    deliveryCheck((string) file_get_contents($loggerBoundaryPath) === $beforeRejectedDiagnostic,
        $label . ' must not append any event bytes');
}

$fixedLogClock = static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-31T00:00:00+00:00');
$templateLogPath = $logDirectory . '/bounded-template.jsonl';
(new OperationalLogger($templateLogPath, $fixedLogClock))->log(
    'success', str_repeat('a', 64), 'success', 200,
);
$boundedNewLine = (string) file_get_contents($templateLogPath);
$boundedMaximum = 240 * 1024;
$exactExistingBytes = $boundedMaximum - strlen($boundedNewLine);
$objectOverhead = strlen("{\"padding\":\"\"}\n");
$exactExistingLine = json_encode([
    'padding' => str_repeat('x', $exactExistingBytes - $objectOverhead),
], JSON_THROW_ON_ERROR) . "\n";
deliveryCheck(strlen($exactExistingLine) === $exactExistingBytes,
    'Exact bounded-log fixture must have its hand-calculated byte length');
$exactBoundaryPath = $logDirectory . '/bounded-exact.jsonl';
file_put_contents($exactBoundaryPath, $exactExistingLine);
chmod($exactBoundaryPath, 0600);
(new OperationalLogger($exactBoundaryPath, $fixedLogClock))->log(
    'success', str_repeat('a', 64), 'success', 200,
);
deliveryCheck(filesize($exactBoundaryPath) === $boundedMaximum,
    'Operational log may reach but never exceed the 240 KiB server bound');

$partialTailPath = $logDirectory . '/partial-tail.jsonl';
$partialPriorHash = str_repeat('6', 64);
$partialNewHash = str_repeat('7', 64);
$partialPriorLine = json_encode([
    'message_id_hash' => $partialPriorHash,
], JSON_THROW_ON_ERROR) . "\n";
file_put_contents($partialTailPath, $partialPriorLine . '{"torn":');
chmod($partialTailPath, 0600);
(new OperationalLogger($partialTailPath, $fixedLogClock))->log(
    'success', $partialNewHash, 'success', 200,
);
$recoveredPartialBytes = (string) file_get_contents($partialTailPath);
deliveryCheck(str_contains($recoveredPartialBytes, $partialPriorHash)
    && str_contains($recoveredPartialBytes, $partialNewHash)
    && !str_contains($recoveredPartialBytes, 'torn'),
    'A partial tail below the size bound must force recovery compaction before appending');
foreach (array_filter(explode("\n", $recoveredPartialBytes)) as $recoveredPartialLine) {
    deliveryCheck(json_decode($recoveredPartialLine, false, 16, JSON_THROW_ON_ERROR) instanceof stdClass,
        'Partial-tail recovery must leave only complete independent JSON-object lines');
}

$retainedHash = str_repeat('b', 64);
$newHash = str_repeat('c', 64);
$seedLines = [];
for ($index = 0; $index < 2200; ++$index) {
    $seedLines[] = json_encode([
        'timestamp' => '2026-07-30T00:00:00+00:00',
        'outcome' => 'failure',
        'message_id_hash' => hash('sha256', 'seed-' . $index),
        'classification' => 'http_error',
        'http_status' => 500,
    ], JSON_THROW_ON_ERROR);
}
$seedLines[] = json_encode([
    'timestamp' => '2026-07-30T00:00:01+00:00',
    'outcome' => 'failure',
    'message_id_hash' => $retainedHash,
    'classification' => 'http_error',
    'http_status' => 500,
], JSON_THROW_ON_ERROR);
$oversizedSeed = implode("\n", $seedLines) . "\nnot-json\n[]\n42\n{\"partial\":";
deliveryCheck(strlen($oversizedSeed) > $boundedMaximum,
    'Compaction fixture must begin above the server log bound');
$compactionPath = $logDirectory . '/bounded-compaction.jsonl';
file_put_contents($compactionPath, $oversizedSeed);
chmod($compactionPath, 0600);
$beforeCompaction = lstat($compactionPath);
(new OperationalLogger($compactionPath, $fixedLogClock))->log(
    'success', $newHash, 'success', 200,
);
$afterCompaction = lstat($compactionPath);
$compactedBytes = (string) file_get_contents($compactionPath);
deliveryCheck(strlen($compactedBytes) <= $boundedMaximum && str_ends_with($compactedBytes, "\n"),
    'Compacted operational log must be bounded and newline terminated');
deliveryCheck(str_contains($compactedBytes, $retainedHash) && str_contains($compactedBytes, $newHash),
    'Compaction must retain the latest existing valid event and the new event');
deliveryCheck(!str_contains($compactedBytes, 'not-json') && !str_contains($compactedBytes, 'partial'),
    'Compaction must discard malformed and partial tail records');
foreach (array_filter(explode("\n", $compactedBytes)) as $compactedLine) {
    deliveryCheck(json_decode($compactedLine, false, 16, JSON_THROW_ON_ERROR) instanceof stdClass,
        'Compaction must retain only complete UTF-8 JSON object lines');
}
deliveryCheck(is_array($beforeCompaction) && is_array($afterCompaction)
    && $beforeCompaction['dev'] === $afterCompaction['dev']
    && $beforeCompaction['ino'] !== $afterCompaction['ino']
    && ($afterCompaction['mode'] & 0777) === 0600
    && $afterCompaction['uid'] === posix_geteuid() && $afterCompaction['nlink'] === 1,
    'Compaction must atomically replace the inode with an owner-only one-link file');

$oversizedEventPath = $logDirectory . '/oversized-event.jsonl';
file_put_contents($oversizedEventPath, "UNCHANGED_OVERSIZED_EVENT\n");
chmod($oversizedEventPath, 0600);
$largeAttempt = new WebhookAttemptDiagnostic(
    500, 'E500', 'server error', 'json', 'application/json', 2, $validBodyHash,
);
try {
    (new OperationalLogger($oversizedEventPath, $fixedLogClock))->log(
        'failure', str_repeat('d', 64), 'http_error', 500,
        new WebhookDiagnostic(array_fill(0, 31_000, $largeAttempt), 10, 3, 4, false),
    );
    throw new RuntimeException('Oversized operational event was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Oversized operational event was accepted',
        'A single event above 120 KiB must be rejected before mutation');
}
deliveryCheck((string) file_get_contents($oversizedEventPath) === "UNCHANGED_OVERSIZED_EVENT\n",
    'Rejected oversized event must leave the existing log unchanged');

$faultPath = $logDirectory . '/bounded-fault.jsonl';
file_put_contents($faultPath, $oversizedSeed);
chmod($faultPath, 0600);
$beforeFault = lstat($faultPath);
$beforeFaultBytes = (string) file_get_contents($faultPath);
try {
    (new OperationalLogger(
        $faultPath,
        $fixedLogClock,
        null,
        static fn (): never => throw new RuntimeException('Injected before truncate'),
    ))->log('success', str_repeat('e', 64), 'success', 200);
    throw new RuntimeException('Compaction fault was not propagated');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Compaction fault was not propagated',
        'Injected compaction failure must become a fixed logging failure');
}
$afterFault = lstat($faultPath);
$faultBytes = (string) file_get_contents($faultPath);
deliveryCheck(is_array($beforeFault) && is_array($afterFault)
    && $beforeFault['size'] === $afterFault['size']
    && $beforeFault['ino'] === $afterFault['ino']
    && hash_equals($beforeFaultBytes, $faultBytes),
    'Failure before atomic replacement must preserve every byte and the inode of the old log');

$wrongModeLockPath = $logDirectory . '/wrong-mode-lock.jsonl';
file_put_contents($wrongModeLockPath . '.lock', 'lock');
chmod($wrongModeLockPath . '.lock', 0644);
try {
    (new OperationalLogger($wrongModeLockPath, $fixedLogClock))->log(
        'success', str_repeat('1', 64), 'success', 200,
    );
    throw new RuntimeException('Wrong-mode sidecar lock was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Wrong-mode sidecar lock was accepted',
        'The fixed sidecar lock must be a mode-0600 regular file');
}
deliveryCheck(!file_exists($wrongModeLockPath),
    'Rejecting a wrong-mode sidecar lock must happen before creating the operational log');

$lockTargetPath = $logDirectory . '/lock-target';
file_put_contents($lockTargetPath, 'UNCHANGED_LOCK_TARGET');
chmod($lockTargetPath, 0600);
$symlinkLockPath = $logDirectory . '/symlink-lock.jsonl';
symlink($lockTargetPath, $symlinkLockPath . '.lock');
try {
    (new OperationalLogger($symlinkLockPath, $fixedLogClock))->log(
        'success', str_repeat('2', 64), 'success', 200,
    );
    throw new RuntimeException('Symlink sidecar lock was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Symlink sidecar lock was accepted',
        'The fixed sidecar lock must reject symlinks');
}
deliveryCheck((string) file_get_contents($lockTargetPath) === 'UNCHANGED_LOCK_TARGET'
    && !file_exists($symlinkLockPath),
    'Rejecting a symlink sidecar lock must not touch its target or create the operational log');

$tempModeFaultPath = $logDirectory . '/temp-mode-fault.jsonl';
file_put_contents($tempModeFaultPath, $oversizedSeed);
chmod($tempModeFaultPath, 0600);
$tempModeFaultBefore = (string) file_get_contents($tempModeFaultPath);
try {
    (new OperationalLogger(
        $tempModeFaultPath,
        $fixedLogClock,
        null,
        null,
        static function (string $phase, ?string $temporaryPath): void {
            if ($phase === 'temp_created' && is_string($temporaryPath)) {
                chmod($temporaryPath, 0644);
            }
        },
    ))->log('success', str_repeat('3', 64), 'success', 200);
    throw new RuntimeException('Wrong-mode temporary file was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Wrong-mode temporary file was accepted',
        'Atomic compaction must reject a temporary file whose mode changes from 0600');
}
deliveryCheck(hash_equals($tempModeFaultBefore, (string) file_get_contents($tempModeFaultPath)),
    'Wrong-mode temporary-file rejection must leave the old log unchanged');

$tempSymlinkFaultPath = $logDirectory . '/temp-symlink-fault.jsonl';
$tempSymlinkTarget = $logDirectory . '/temp-symlink-target';
file_put_contents($tempSymlinkFaultPath, $oversizedSeed);
chmod($tempSymlinkFaultPath, 0600);
file_put_contents($tempSymlinkTarget, 'UNCHANGED_TEMP_SYMLINK_TARGET');
chmod($tempSymlinkTarget, 0600);
$tempSymlinkBefore = (string) file_get_contents($tempSymlinkFaultPath);
try {
    (new OperationalLogger(
        $tempSymlinkFaultPath,
        $fixedLogClock,
        null,
        null,
        static function (string $phase, ?string $temporaryPath) use ($tempSymlinkTarget): void {
            if ($phase === 'temp_created' && is_string($temporaryPath)) {
                unlink($temporaryPath);
                symlink($tempSymlinkTarget, $temporaryPath);
            }
        },
    ))->log('success', str_repeat('a', 64), 'success', 200);
    throw new RuntimeException('Symlink temporary path was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Symlink temporary path was accepted',
        'Atomic compaction must reject replacement of its temporary name by a symlink');
}
deliveryCheck(hash_equals($tempSymlinkBefore, (string) file_get_contents($tempSymlinkFaultPath))
    && (string) file_get_contents($tempSymlinkTarget) === 'UNCHANGED_TEMP_SYMLINK_TARGET',
    'Temporary symlink rejection must preserve both the old log and symlink target');

$shortWritePath = $logDirectory . '/short-write-fault.jsonl';
file_put_contents($shortWritePath, $oversizedSeed);
chmod($shortWritePath, 0600);
$shortWriteBefore = (string) file_get_contents($shortWritePath);
$shortWriteCalls = 0;
try {
    (new OperationalLogger(
        $shortWritePath,
        $fixedLogClock,
        null,
        null,
        null,
        static function ($handle, string $bytes) use (&$shortWriteCalls): int|false {
            ++$shortWriteCalls;
            if ($shortWriteCalls === 1) {
                return fwrite($handle, substr($bytes, 0, 17));
            }
            return false;
        },
    ))->log('success', str_repeat('4', 64), 'success', 200);
    throw new RuntimeException('Partial temporary write was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Partial temporary write was accepted',
        'A short then failed temporary write must abort atomic compaction');
}
deliveryCheck($shortWriteCalls >= 2
    && hash_equals($shortWriteBefore, (string) file_get_contents($shortWritePath)),
    'A partial temporary write must leave the old log unchanged');

$flushFaultPath = $logDirectory . '/flush-fault.jsonl';
file_put_contents($flushFaultPath, $oversizedSeed);
chmod($flushFaultPath, 0600);
$flushFaultBefore = (string) file_get_contents($flushFaultPath);
try {
    (new OperationalLogger(
        $flushFaultPath,
        $fixedLogClock,
        null,
        null,
        static function (string $phase): void {
            if ($phase === 'before_temp_flush') {
                throw new RuntimeException('Injected flush fault');
            }
        },
    ))->log('success', str_repeat('5', 64), 'success', 200);
    throw new RuntimeException('Temporary flush fault was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Temporary flush fault was accepted',
        'A temporary flush fault must abort before atomic replacement');
}
deliveryCheck(hash_equals($flushFaultBefore, (string) file_get_contents($flushFaultPath)),
    'A temporary flush fault must leave the old log unchanged');

$afterRenameFaultPath = $logDirectory . '/after-rename-fault.jsonl';
file_put_contents($afterRenameFaultPath, $oversizedSeed);
chmod($afterRenameFaultPath, 0600);
$afterRenameHash = str_repeat('f', 64);
try {
    (new OperationalLogger(
        $afterRenameFaultPath,
        $fixedLogClock,
        null,
        null,
        static function (string $phase): void {
            if ($phase === 'after_rename') {
                throw new RuntimeException('Injected post-rename interruption');
            }
        },
    ))->log('success', $afterRenameHash, 'success', 200);
} catch (RuntimeException) {
}
$afterRenameBytes = (string) file_get_contents($afterRenameFaultPath);
deliveryCheck(str_ends_with($afterRenameBytes, "\n")
    && str_contains($afterRenameBytes, $afterRenameHash)
    && !str_contains($afterRenameBytes, 'partial'),
    'An interruption after rename must expose the complete new JSONL file, never a torn replacement');
foreach (array_filter(explode("\n", $afterRenameBytes)) as $afterRenameLine) {
    deliveryCheck(json_decode($afterRenameLine, false, 16, JSON_THROW_ON_ERROR) instanceof stdClass,
        'Every line visible after a post-rename interruption must remain a complete JSON object');
}

$restartPath = $logDirectory . '/restart-after-fault.jsonl';
file_put_contents($restartPath, $partialPriorLine . '{"interrupted":');
chmod($restartPath, 0600);
try {
    (new OperationalLogger(
        $restartPath,
        $fixedLogClock,
        null,
        null,
        static function (string $phase): void {
            if ($phase === 'before_rename') {
                throw new RuntimeException('Injected rename fault');
            }
        },
    ))->log('success', str_repeat('8', 64), 'success', 200);
} catch (RuntimeException) {
}
(new OperationalLogger($restartPath, $fixedLogClock))->log(
    'success', str_repeat('9', 64), 'success', 200,
);
$restartBytes = (string) file_get_contents($restartPath);
deliveryCheck(!str_contains($restartBytes, 'interrupted')
    && str_contains($restartBytes, str_repeat('9', 64)),
    'A new logger process must recover a pre-rename partial-tail failure into complete JSONL');

if (function_exists('pcntl_fork') && function_exists('pcntl_waitpid')) {
    $parallelPath = $logDirectory . '/parallel-writers.jsonl';
    file_put_contents($parallelPath, $partialPriorLine . '{"interrupted":');
    chmod($parallelPath, 0600);
    $children = [];
    for ($worker = 0; $worker < 2; ++$worker) {
        $child = pcntl_fork();
        if ($child === 0) {
            try {
                for ($entry = 0; $entry < 8; ++$entry) {
                    (new OperationalLogger($parallelPath, $fixedLogClock))->log(
                        'success', hash('sha256', 'parallel-' . $worker . '-' . $entry), 'success', 200,
                    );
                }
                exit(0);
            } catch (Throwable) {
                exit(1);
            }
        }
        deliveryCheck(is_int($child) && $child > 0, 'Parallel writer process must start');
        $children[] = $child;
    }
    foreach ($children as $child) {
        $status = 0;
        deliveryCheck(pcntl_waitpid($child, $status) === $child && pcntl_wifexited($status)
            && pcntl_wexitstatus($status) === 0, 'Parallel writer process must complete successfully');
    }
    $parallelLines = array_values(array_filter(explode("\n", (string) file_get_contents($parallelPath))));
    deliveryCheck(count($parallelLines) === 17,
        'Fixed sidecar locking must retain the prior event and all 16 parallel appends exactly once');
    foreach ($parallelLines as $parallelLine) {
        deliveryCheck(json_decode($parallelLine, false, 16, JSON_THROW_ON_ERROR) instanceof stdClass,
            'Parallel writers must leave only complete JSON-object lines');
    }
}

$wrongOwnerPath = $logDirectory . '/wrong-owner.jsonl';
file_put_contents($wrongOwnerPath, "UNCHANGED_WRONG_OWNER\n");
chmod($wrongOwnerPath, 0600);
$wrongOwnerLogger = new OperationalLogger(
    $wrongOwnerPath,
    null,
    static fn (): int => posix_geteuid() + 1,
);
try {
    $wrongOwnerLogger->log('success', str_repeat('8', 64), 'success', 200);
    throw new RuntimeException('Wrong-owner operational log was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Wrong-owner operational log was accepted',
        'OperationalLogger must compare stat ownership with the effective UID resolver');
}
deliveryCheck((string) file_get_contents($wrongOwnerPath) === "UNCHANGED_WRONG_OWNER\n"
    && (fileperms($wrongOwnerPath) & 0777) === 0600,
    'Rejected wrong-owner log must not be modified or chmodded');

$badModePath = $logDirectory . '/bad-mode.jsonl';
file_put_contents($badModePath, "UNCHANGED_BAD_MODE\n");
chmod($badModePath, 0644);
try {
    (new OperationalLogger($badModePath))->log('success', str_repeat('2', 64), 'success', 200);
    throw new RuntimeException('Mode-0644 operational log was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Mode-0644 operational log was accepted',
        'Existing mode-0644 operational log must be rejected');
}
deliveryCheck((string) file_get_contents($badModePath) === "UNCHANGED_BAD_MODE\n"
    && (fileperms($badModePath) & 0777) === 0644,
    'Rejected mode-0644 log must not be modified or chmodded');

$outsideDirectory = sys_get_temp_dir() . '/delivery-log-target-' . bin2hex(random_bytes(8));
mkdir($outsideDirectory, 0700);
$outsideTarget = $outsideDirectory . '/outside.jsonl';
file_put_contents($outsideTarget, "UNCHANGED_SYMLINK_TARGET\n");
chmod($outsideTarget, 0600);
$symlinkPath = $logDirectory . '/symlink.jsonl';
symlink($outsideTarget, $symlinkPath);
try {
    (new OperationalLogger($symlinkPath))->log('success', str_repeat('3', 64), 'success', 200);
    throw new RuntimeException('Symlink operational log was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Symlink operational log was accepted',
        'Symlink operational log must be rejected');
}
deliveryCheck((string) file_get_contents($outsideTarget) === "UNCHANGED_SYMLINK_TARGET\n"
    && (fileperms($outsideTarget) & 0777) === 0600,
    'Rejected symlink must not alter or chmod its external target');

$openParent = $logDirectory . '/open-parent';
mkdir($openParent, 0755);
$openParentLog = $openParent . '/operational.jsonl';
try {
    (new OperationalLogger($openParentLog))->log('success', str_repeat('4', 64), 'success', 200);
    throw new RuntimeException('Operational log below a non-0700 parent was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Operational log below a non-0700 parent was accepted',
        'Operational log parent must be mode 0700');
}
deliveryCheck(!file_exists($openParentLog), 'Rejected non-private parent must not receive a log file');

$publicLogDirectory = $logDirectory . '/public_html';
mkdir($publicLogDirectory, 0700);
$publicLogPath = $publicLogDirectory . '/operational.jsonl';
try {
    (new OperationalLogger($publicLogPath))->log('success', str_repeat('5', 64), 'success', 200);
    throw new RuntimeException('Operational log below public_html was accepted');
} catch (RuntimeException $exception) {
    deliveryCheck($exception->getMessage() !== 'Operational log below public_html was accepted',
        'Operational log path must remain outside public_html');
}
deliveryCheck(!file_exists($publicLogPath), 'Rejected public path must not receive a log file');

unlink($logPath);
unlink($echoLogPath);
unlink($loggerBoundaryPath);
unlink($templateLogPath);
unlink($exactBoundaryPath);
unlink($partialTailPath);
unlink($compactionPath);
unlink($oversizedEventPath);
unlink($faultPath);
unlink($wrongModeLockPath . '.lock');
unlink($symlinkLockPath . '.lock');
unlink($lockTargetPath);
unlink($tempModeFaultPath);
unlink($tempSymlinkFaultPath);
unlink($tempSymlinkTarget);
unlink($shortWritePath);
unlink($flushFaultPath);
unlink($afterRenameFaultPath);
unlink($restartPath);
if (isset($parallelPath) && file_exists($parallelPath)) {
    unlink($parallelPath);
}
unlink($wrongOwnerPath);
unlink($badModePath);
unlink($symlinkPath);
unlink($outsideTarget);
foreach (glob($logDirectory . '/*.lock') ?: [] as $operationalLockPath) {
    unlink($operationalLockPath);
}
foreach (glob($logDirectory . '/.*.tmp.*') ?: [] as $operationalTemporaryPath) {
    unlink($operationalTemporaryPath);
}
rmdir($outsideDirectory);
rmdir($openParent);
rmdir($publicLogDirectory);
rmdir($logDirectory);

foreach ([0, -1, 31] as $invalidSoftCap) {
    try {
        NotifierConfig::fromArray([
            'webhook_url' => 'https://webhook.worksmobile.com/message/test',
            'error_recipients' => ['operator@example.invalid'],
            'notification_pinned_targets' => [],
            'notification_targets' => ['target@example.invalid'],
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
    'notification_pinned_targets' => [],
    'notification_targets' => ['target@example.invalid'],
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

$recipientGateDirectory = sys_get_temp_dir() . '/recipient-gate-' . bin2hex(random_bytes(8));
mkdir($recipientGateDirectory, 0700);
$recipientGateLog = $recipientGateDirectory . '/delivery.log';
$recipientGateDedupPath = $recipientGateDirectory . '/claims.json';
$recipientGateConfig = NotifierConfig::fromArray([
    'webhook_url' => 'https://webhook.worksmobile.com/message/test',
    'error_recipients' => ['operator@example.invalid'],
    'notification_pinned_targets' => ['info@example.invalid'],
    'notification_targets' => [
        'alerts@example.invalid',
        'forwarding@example.invalid',
        'info@example.invalid',
    ],
    'system_mail_hmac_key' => $configKey,
    'log_path' => $recipientGateLog,
    'dedup_path' => $recipientGateDedupPath,
]);
$recipientGateRequests = [];
$recipientGateErrorRequests = [];
$recipientGateLogger = new OperationalLogger($recipientGateLog);
$recipientGateApplication = new DeliveryApplication(
    new WebhookClient(
        'https://webhook.worksmobile.com/message/test',
        static function () use (&$recipientGateRequests): array {
            $recipientGateRequests[] = true;
            return response(200, 'success');
        },
    ),
    new ErrorReporter(
        new WebhookClient(
            'https://webhook.worksmobile.com/message/error-test',
            static function () use (&$recipientGateErrorRequests): array {
                $recipientGateErrorRequests[] = true;
                return response(500, 'server error');
            },
        ),
        $recipientGateLogger,
    ),
    $recipientGateLogger,
    $recipientGateConfig,
    new DeliveryDeduplicator($recipientGateDedupPath),
);

$recipientGateCase = static function (
    string $headers,
    bool $expectedDelivery,
    string $label,
    string $body = 'Body',
) use (&$recipientGateRequests, &$recipientGateErrorRequests, $recipientGateApplication): void {
    static $messageNumber = 0;
    ++$messageNumber;
    $before = count($recipientGateRequests);
    $raw = "From: sender@example.invalid\r\n"
        . $headers . "\r\n"
        . 'Message-ID: <recipient-matrix-' . $messageNumber . "@example.invalid>\r\n"
        . "Subject: Recipient matrix\r\n\r\n" . $body;
    $recipientGateApplication->deliver($raw);
    deliveryCheck(count($recipientGateRequests) - $before === ($expectedDelivery ? 1 : 0), $label);
    deliveryCheck($recipientGateErrorRequests === [], $label . ' must not create an incident');
};

foreach ($recipientGateConfig->notificationTargets as $target) {
    $recipientGateCase('To: ' . $target, true, 'Each configured direct To target must deliver once');
}
$recipientGateCase("To: employee@example.invalid\r\nCc: info@example.invalid", true,
    'Configured Cc with another To must deliver once');
$recipientGateCase('To: "日本語表示名" <info@example.invalid>', true,
    'Display-name target must deliver once');
$recipientGateCase("To: employee@example.invalid\r\nCc: employee2@example.invalid,\r\n info@example.invalid", true,
    'Folded Cc target must deliver once');
$recipientGateCase('To: Team: employee@example.invalid, info@example.invalid;', true,
    'Group address target must deliver once');
$recipientGateCase('To: INFO@EXAMPLE.INVALID', true,
    'Case-only target difference must deliver once');
$recipientGateCase("To: info@example.invalid\r\nCc: alerts@example.invalid", true,
    'Multiple matching visible recipients must still deliver once');

foreach ([
    "To: employee@example.invalid\r\nReply-To: info@example.invalid" => 'Reply-To-only target must not deliver',
    "To: employee@example.invalid\r\nBcc: info@example.invalid" => 'Bcc-only target must not deliver',
    "To: employee@example.invalid\r\nDelivered-To: info@example.invalid" => 'Delivered-To-only target must not deliver',
    "To: employee@example.invalid\r\nX-Original-To: info@example.invalid" => 'X-Original-To-only target must not deliver',
    "To: employee@example.invalid\r\nReturn-Path: <info@example.invalid>" => 'Return-Path-only target must not deliver',
    "To: employee@example.invalid\r\nReceived: from info@example.invalid by mx.example.invalid" => 'Received-only target must not deliver',
    "To: employee@example.invalid\r\nReceived: from mx.example.invalid (mx.example.invalid [192.0.2.1])\r\n by inbound.example.invalid with ESMTPS id abc123\r\n for <info@example.invalid>; Wed, 15 Jul 2026 12:34:56 +0900" => 'Complete Received trace target must not deliver',
    'From: info@example.invalid' => 'From-only target must not deliver',
    'To: prefixinfo@example.invalid' => 'Partial address match must not deliver',
    'To: employee@example.invalid (info@example.invalid)' => 'Comment-only address text must not deliver',
] as $headers => $label) {
    $recipientGateCase($headers, false, $label);
}
$recipientGateCase("To: employee@example.invalid\r\nSubject: info@example.invalid", false,
    'Subject-only target must not deliver');
$recipientGateCase('To: employee@example.invalid', false,
    'Body-only target must not deliver', 'Body contains info@example.invalid only');

$recipientGateEvents = array_values(array_filter(explode("\n", (string) file_get_contents($recipientGateLog))));
$ignoredEvents = array_values(array_filter($recipientGateEvents, static function (string $line): bool {
    $event = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
    return $event['outcome'] === 'ignored' && $event['classification'] === 'non_target_recipient';
}));
deliveryCheck(count($ignoredEvents) === 12, 'Every normal non-target case must create one ignored event');
foreach ($ignoredEvents as $line) {
    deliveryCheck(!str_contains($line, '@') && !str_contains($line, 'Recipient matrix')
        && !str_contains($line, 'Body contains'),
        'Ignored logs must contain metadata only, never addresses, subjects, or bodies');
}

$ignoredLogFailureRequests = [];
$ignoredLogFailureErrorRequests = [];
$ignoredLogFailureDedupPath = $recipientGateDirectory . '/ignored-log-failure.json';
$ignoredLogFailureLogger = new OperationalLogger($recipientGateDirectory . '/missing/delivery.log');
(new DeliveryApplication(
    new WebhookClient(
        'https://webhook.worksmobile.com/message/test',
        static function () use (&$ignoredLogFailureRequests): array {
            $ignoredLogFailureRequests[] = true;
            return response(200, 'success');
        },
    ),
    new ErrorReporter(
        new WebhookClient(
            'https://webhook.worksmobile.com/message/error-test',
            static function () use (&$ignoredLogFailureErrorRequests): array {
                $ignoredLogFailureErrorRequests[] = true;
                return response(500, 'server error');
            },
        ),
        $ignoredLogFailureLogger,
    ),
    $ignoredLogFailureLogger,
    $recipientGateConfig,
    new DeliveryDeduplicator($ignoredLogFailureDedupPath),
))->deliver("From: sender@example.invalid\r\nTo: employee@example.invalid\r\nMessage-ID: <ignored-log-failure@example.invalid>\r\n\r\nBody");
deliveryCheck($ignoredLogFailureRequests === [] && $ignoredLogFailureErrorRequests === [],
    'Ignored-log failure must not deliver or create an incident');
deliveryCheck(!file_exists($ignoredLogFailureDedupPath),
    'Ignored-log failure must not reserve deduplication state');

foreach (glob($recipientGateDirectory . '/*') ?: [] as $file) { if (is_file($file)) unlink($file); }
foreach (glob($recipientGateDirectory . '/.*') ?: [] as $file) { if (is_file($file)) unlink($file); }
rmdir($recipientGateDirectory);

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
    return $retryCalls <= 2 ? response(500, 'server error') : response(200, 'success');
}, 32_768, static function (): void {});
$retryReporter = new ErrorReporter(
    new WebhookClient(
        'https://webhook.worksmobile.com/message/test',
        static fn (): array => response(200, 'success'),
    ),
    $dedupLogger,
);
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
    return $reporterDeliveryCalls <= 2 ? response(500, 'server error') : response(200, 'success');
}, 32_768, static function (): void {});
$reporterRetryRaw = str_replace('<same@example.invalid>', '<reporter-retry@example.invalid>', $sameRaw);
$reporterRetryApplication = new DeliveryApplication($reporterDeliveryWebhook, $throwingReporter, $dedupLogger, null, $appDedup);
$reporterRetryApplication->deliver($reporterRetryRaw);
$reporterRetryApplication->deliver($reporterRetryRaw);
deliveryCheck($reporterDeliveryCalls === 3, 'Reporter failure must not prevent reservation release and later retry');

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
$reportFailureDirectory = sys_get_temp_dir() . '/report-failure-log-' . bin2hex(random_bytes(8));
mkdir($reportFailureDirectory, 0700);
$reportFailureLog = $reportFailureDirectory . '/operational.jsonl';
file_put_contents($reportFailureLog, '');
chmod($reportFailureLog, 0600);
(new DeliveryApplication($reportFailureWebhook, $reportFailureReporter, new OperationalLogger($reportFailureLog)))
    ->deliver(file_get_contents(dirname(__DIR__) . '/fixtures/plain.eml') ?: '');
deliveryCheck($reportFailureCalls === 2, 'Reporter failure must be swallowed without retrying the reporter');
unlink($reportFailureLog);
rmdir($reportFailureDirectory);

$forcedRaw = "From: sender@example.invalid\r\nTo: target@example.invalid\r\nSubject: [Error Test {$testToken}]\r\nMessage-ID: <forced@example.invalid>\r\n\r\nSecret body";

$healthDirectory = sys_get_temp_dir() . '/delivery-health-integration-' . bin2hex(random_bytes(8));
mkdir($healthDirectory, 0700);
$healthDirectory = realpath($healthDirectory);
deliveryCheck(is_string($healthDirectory), 'Health integration directory must resolve');
$healthLog = $healthDirectory . '/operational-LOG_PATH_MARKER.jsonl';
$healthAuth = new SystemMailAuthenticator(str_pad('HMAC_KEY_MARKER', 32, '_'));
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
    'https://webhook.example.invalid/PRIVATE_WEBHOOK_MARKER',
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
    . "Cc: ORIGINAL_CC_MARKER@example.invalid\r\n"
    . "Bcc: ORIGINAL_BCC_MARKER@example.invalid\r\n"
    . "Subject: ORIGINAL_SUBJECT_MARKER\r\n"
    . "Message-ID: <sensitive@example.invalid>\r\n"
    . "Content-Type: multipart/mixed; boundary=privacy\r\n\r\n"
    . "--privacy\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nORIGINAL_BODY_MARKER\r\n"
    . "--privacy\r\nContent-Type: application/octet-stream\r\n"
    . "Content-Disposition: attachment; filename=ORIGINAL_ATTACHMENT_MARKER.txt\r\n\r\nattachment\r\n"
    . "--privacy--\r\n";
$observedApplication->deliver($sensitiveRaw);
deliveryCheck($observedCalls === 2 && $healthMonitor->status() === 'degraded'
    && count($healthSendmailAdapter->messages) === 1,
    'Normal failure plus error-webhook failure must produce one outage transition');
$degradedIntegration = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($degradedIntegration['next_observation_sequence'] === 2
    && $degradedIntegration['last_applied_sequence'] === 2,
    'Normal and error webhooks must reserve separate observations and apply only the error result');
$expectedRealErrorBody = "LINE WORKSへのメール通知で障害が発生しました。\n"
    . "復旧するまで、LINE WORKSへ通知されない可能性があります。\n\n"
    . "【必要な対応】\nXserverのメールボックスで新着メールを直接確認してください。\n"
    . "このメールへの返信は不要です。\n\n"
    . "障害発生日時：2026年07月13日（月）21時00分00秒\n"
    . "障害内容：LINE WORKSに接続できませんでした。\n\n"
    . "【管理者向け情報】\n原因コード：transport_error\n"
    . "確認方法：Macの「Xserverメール通知管理」アプリで「同期診断」を実行してください。\n";
$decodedRealError = decodeDeliverySystemWire($healthSendmailAdapter->messages[0]);
deliveryCheck($decodedRealError['subject'] === '【要確認】LINE WORKSメール通知で障害が発生しました'
    && $decodedRealError['body'] === $expectedRealErrorBody,
    'Integrated outage subject and body must match the approved Japanese copy');
$observedApplication->deliver($otherRaw);
deliveryCheck($observedCalls === 3 && $healthMonitor->status() === 'healthy'
    && count($healthSendmailAdapter->messages) === 2,
    'Next normal webhook success must produce one recovery transition');
$healthyIntegration = json_decode((string) file_get_contents(
    $healthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($healthyIntegration['next_observation_sequence'] === 3
    && $healthyIntegration['last_applied_sequence'] === 3,
    'Logical success must reserve and apply exactly one observation');
$expectedRealRecoveryBody = "LINE WORKSへのメール通知は復旧しました。\n"
    . "今後受信する対象メールは通常どおり通知されます。\n\n"
    . "障害中にLINE WORKSへ通知されなかったメールは自動では再通知されません。\n\n"
    . "【必要な対応】\n障害発生日時から復旧日時までの新着メールを、\n"
    . "Xserverのメールボックスで確認してください。\nこのメールへの返信は不要です。\n\n"
    . "復旧日時：2026年07月13日（月）21時00分00秒\n"
    . "障害発生日時：2026年07月13日（月）21時00分00秒\n"
    . "障害内容：LINE WORKSに接続できませんでした。\n現在の状態：正常\n\n"
    . "【管理者向け情報】\n原因コード：transport_error\n";
$decodedRealRecovery = decodeDeliverySystemWire($healthSendmailAdapter->messages[1]);
deliveryCheck($decodedRealRecovery['subject'] === '【復旧・要確認】LINE WORKSメール通知が復旧しました'
    && $decodedRealRecovery['body'] === $expectedRealRecoveryBody,
    'Integrated recovery subject and body must match the approved Japanese copy');
$sensitiveHash = hash('sha256', 'sensitive@example.invalid');
foreach ($healthSendmailAdapter->messages as $message) {
    $decodedMessage = decodeDeliverySystemWire($message);
    deliveryCheck(($decodedMessage['headers']['to'] ?? '') === 'operator@example.invalid',
        'Integrated system mail To must contain only the configured notification recipient');
    foreach (['ORIGINAL_FROM_MARKER', 'ORIGINAL_TO_MARKER', 'ORIGINAL_CC_MARKER',
        'ORIGINAL_BCC_MARKER', 'ORIGINAL_SUBJECT_MARKER', 'ORIGINAL_BODY_MARKER',
        'ORIGINAL_ATTACHMENT_MARKER', 'PRIVATE_WEBHOOK_MARKER', 'EXCEPTION_MARKER',
        'HMAC_KEY_MARKER', 'LOG_PATH_MARKER', $sensitiveHash] as $privateMarker) {
        deliveryCheck(!str_contains($message, $privateMarker)
            && !str_contains($decodedMessage['subject'], $privateMarker)
            && !str_contains($decodedMessage['body'], $privateMarker),
            'System mail raw wire, decoded subject, and decoded body must omit integration sentinels');
    }
}

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
$forcedApplication = new DeliveryApplication(
    $forcedObservedWebhook, $forcedObservedReporter, new OperationalLogger($forcedHealthLog),
    $armedConfig, null, $healthAuth, $forcedHealthMonitor,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
);
$forcedApplication->deliver($forcedRaw);
deliveryCheck($forcedObservedCalls === 0 && $forcedHealthMonitor->status() === 'degraded'
    && count($forcedHealthAdapter->messages) === 1,
    'Token-matched forced test must reserve one synthetic failure with zero HTTP');
$forcedState = json_decode((string) file_get_contents(
    $forcedHealthDirectory . '/delivery-health.json'), true, 16, JSON_THROW_ON_ERROR);
deliveryCheck($forcedState['next_observation_sequence'] === 1
    && $forcedState['last_applied_sequence'] === 1,
    'Forced test must reserve and apply exactly one synthetic observation');
$forcedApplication->deliver($otherRaw);
deliveryCheck($forcedObservedCalls === 1 && $forcedHealthMonitor->status() === 'healthy'
    && count($forcedHealthAdapter->messages) === 2,
    'Ordinary successful delivery after forced outage must produce one test recovery transition');
$expectedTestErrorBody = "これは管理者による障害通知メールの動作確認です。\n"
    . "実際の障害ではありません。対応は不要です。\n\n"
    . "テスト実行日時：2026年07月13日（月）21時00分00秒\n"
    . "確認結果：障害通知メールを正常に送信しました。\n\n"
    . "【管理者向け情報】\n原因コード：forced_test_failure\n";
$expectedTestRecoveryBody = "これは管理者による復旧通知メールの動作確認です。\n"
    . "実際の障害ではありません。対応は不要です。\n\n"
    . "テスト実行日時：2026年07月13日（月）21時00分00秒\n"
    . "確認結果：復旧通知メールを正常に送信しました。\n\n"
    . "【管理者向け情報】\n原因コード：forced_test_failure\n";
$decodedTestError = decodeDeliverySystemWire($forcedHealthAdapter->messages[0]);
$decodedTestRecovery = decodeDeliverySystemWire($forcedHealthAdapter->messages[1]);
deliveryCheck($decodedTestError['subject'] === '【テスト・対応不要】障害通知メールの動作確認'
    && $decodedTestError['body'] === $expectedTestErrorBody,
    'Forced outage subject and body must match the approved Japanese copy');
deliveryCheck($decodedTestRecovery['subject'] === '【テスト・対応不要】復旧通知メールの動作確認'
    && $decodedTestRecovery['body'] === $expectedTestRecoveryBody,
    'Forced recovery subject and body must match the approved Japanese copy');

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
$preflightAuth = new SystemMailAuthenticator(str_repeat('k', 32));
$preflightApplication = new DeliveryApplication(
    $preflightWebhook, $preflightReporter, $preflightLogger, null,
    new DeliveryDeduplicator($preflightDedupPath), $preflightAuth, $preflightMonitor,
    static fn (): DateTimeImmutable => new DateTimeImmutable('2026-07-13T12:00:00+00:00'),
    static function () use (&$preflightParser): never { ++$preflightParser; throw new RuntimeException('parser called'); },
);
$systemWire = $preflightAuth->build(
    'error', ['operator@example.invalid'], 'Mon, 13 Jul 2026 21:00:00 +0900',
    str_repeat('7', 32), "統合テスト本文\n", false,
);
$legacySystemWire = file_get_contents(dirname(__DIR__) . '/fixtures/system-mail-v1-postfix.eml');
deliveryCheck(is_string($legacySystemWire), 'Fixed Postfix v1 fixture must be readable for delivery preflight');
$legacySystemWire = str_replace("\n", "\r\n", str_replace("\r\n", "\n", $legacySystemWire));
foreach ([$systemWire, $legacySystemWire] as $authenticSystemWire) {
    $preflightApplication->deliver($authenticSystemWire);
}
deliveryCheck($preflightParser === 0 && $preflightHttp === 0 && $preflightAdapter->messages === []
    && !file_exists($preflightDedupPath)
    && !file_exists($preflightDirectory . '/delivery-health.json'),
    'Authentic system mail must stop before parser, dedup, health, webhook, and sendmail');
$preflightEvents = array_map(
    static fn (string $line): array => json_decode($line, true, 16, JSON_THROW_ON_ERROR),
    array_values(array_filter(explode("\n", (string) file_get_contents($preflightLog)))),
);
deliveryCheck(count($preflightEvents) === 2
    && array_column($preflightEvents, 'classification') === ['system_mail_suppressed', 'system_mail_suppressed'],
    'Authentic v2 and fixed Postfix v1 system mail must emit only suppression logs');

foreach ([
    'missing' => str_replace("X-Xserver-Mail-Notifier-Auth: ", "X-Removed-Auth: ", $systemWire),
    'duplicate' => "X-Xserver-Mail-Notifier-Type: error\r\n" . $systemWire,
    'body-mutated' => $systemWire . 'changed',
    'replay-mutated' => $preflightAuth->build('recovery', ['operator@example.invalid'],
        'Mon, 13 Jul 2026 21:00:00 +0900', str_repeat('9', 32), "different body\n", false) . 'changed',
] as $forgeryName => $forgedWire) {
    $callsBefore = $preflightHttp;
    $ordinaryApplication = new DeliveryApplication(
        $preflightWebhook, $preflightReporter, $preflightLogger, null, null,
        $preflightAuth, $preflightMonitor,
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
