<?php

declare(strict_types=1);

use XserverMail\ErrorReporter;
use XserverMail\DeliveryApplication;
use XserverMail\DeliveryDeduplicator;
use XserverMail\DeliveryHealthMonitor;
use XserverMail\NativePrivateStateFilesystem;
use XserverMail\NativeSendmailProcessAdapter;
use XserverMail\OperationalLogger;
use XserverMail\NotifierConfig;
use XserverMail\SendmailClient;
use XserverMail\SystemMailAuthenticator;
use XserverMail\WebhookClient;

if (getenv('MAIL_NOTIFIER_FD_RUNTIME') !== '1') {
    require dirname(__DIR__) . '/vendor/autoload.php';
}

$arguments = array_slice($argv, 1);
$listMode = $arguments === ['--outbox-list'];
$retryMode = count($arguments) === 3 && $arguments[0] === '--outbox-retry'
    && preg_match('/\A[a-f0-9]{64}\z/D', $arguments[1]) === 1
    && preg_match('/\A[a-f0-9]{64}\z/D', $arguments[2]) === 1;
if (!$listMode && !$retryMode && !in_array($arguments, [[], ['--check-config'], ['--check-message']], true)) {
    exit(1);
}
$checkMode = $arguments === ['--check-config'];
$messageCheckMode = $arguments === ['--check-message'];
$exitCode = 0;

try {
    $framed = getenv('MAIL_NOTIFIER_STDIN_FRAME') === '1';
    if ($framed) {
        $frame = XserverMail\StdinFrame::decode(STDIN);
        $value = json_decode($frame['configJson'], true, 32, JSON_THROW_ON_ERROR);
        if (!is_array($value) || array_is_list($value)) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        $config = NotifierConfig::fromArray($value);
        $raw = $frame['message'];
    } else {
        $config = NotifierConfig::load(getenv('MAIL_NOTIFIER_CONFIG') ?: NotifierConfig::defaultPath(__DIR__));
    }
    if ($checkMode) {
        exit(0);
    }
    if ($listMode) {
        $outbox = new XserverMail\DeliveryOutbox(dirname($config->logPath) . '/delivery-outbox.json');
        echo json_encode($outbox->listMetadata(), JSON_THROW_ON_ERROR) . "\n";
        exit(0);
    }
    if ($messageCheckMode) {
        if (!$framed) {
            $raw = stream_get_contents(STDIN, 10 * 1024 * 1024 + 1);
            if (!is_string($raw) || strlen($raw) > 10 * 1024 * 1024) {
                throw new RuntimeException('Input unavailable');
            }
        }
        (new XserverMail\MailParser())->parse($raw, new DateTimeImmutable('2000-01-01T00:00:00+09:00'));
        exit(0);
    }
    $logger = new OperationalLogger($config->logPath);
    $authenticator = new SystemMailAuthenticator($config->systemMailHmacKey);
    $healthMonitor = new DeliveryHealthMonitor(
        $config->healthPath,
        $config->errorRecipients,
        $config->logPath,
        $authenticator,
        new SendmailClient(new NativeSendmailProcessAdapter()),
        $logger,
        new NativePrivateStateFilesystem(),
        static fn (): DateTimeImmutable => new DateTimeImmutable('now', new DateTimeZone('UTC')),
        static fn (): string => bin2hex(random_bytes(16)),
    );
    $webhook = new WebhookClient(
        $config->webhookUrl, null, $config->softCapBytes, null, $healthMonitor,
    );
    $reporter = new ErrorReporter(
        $webhook, $logger, $healthMonitor,
    );
    $outbox = null;
    try {
        $outbox = new XserverMail\DeliveryOutbox(dirname($config->logPath) . '/delivery-outbox.json');
    } catch (Throwable) {
        if ($retryMode) {
            throw new RuntimeException('Private recovery unavailable');
        }
        try {
            $logger->log('failure', hash('sha256', 'startup'), 'outbox_store_failure', null);
        } catch (Throwable) {
        }
    }
    if ($retryMode) {
        $result = (new XserverMail\DeliveryRecovery($outbox, $config, $webhook, $logger, $healthMonitor))
            ->retrySelected($arguments[1], $arguments[2]);
        echo json_encode($result, JSON_THROW_ON_ERROR) . "\n";
        exit(0);
    }
    $deduplicator = new DeliveryDeduplicator($config->dedupPath);

    if (!$framed) {
        $raw = '';
        $limit = (10 * 1024 * 1024) + 1;
        while (!feof(STDIN) && strlen($raw) < $limit) {
            $part = fread(STDIN, min(8192, $limit - strlen($raw)));
            if ($part === false) {
                throw new RuntimeException('Input unavailable');
            }
            $raw .= $part;
        }
    }

    (new DeliveryApplication(
        $webhook, $reporter, $logger, $config, $deduplicator,
        $authenticator, $healthMonitor, null, null, $outbox,
    ))->deliver($raw);
} catch (Throwable $error) {
    if ($listMode || $retryMode) {
        fwrite(STDERR, "未送信通知の確認・再送を完了できませんでした。一覧で状態を再確認してください。\n");
        exit(1);
    }
    if (!isset($reporter)) {
        $exitCode = 1;
    } else {
        try {
            if (!$checkMode && !$messageCheckMode) {
                $reporter->report($error, hash('sha256', 'startup'), false, 'startup');
            }
        } catch (Throwable) {
            // Reporting must not block inbound mail delivery.
        }
        $exitCode = ($checkMode || $messageCheckMode) ? 1 : 0;
    }
}

exit($exitCode);
