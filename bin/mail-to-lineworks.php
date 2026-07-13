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
if (!in_array($arguments, [[], ['--check-config'], ['--check-message']], true)) {
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
    $deduplicator = new DeliveryDeduplicator($config->dedupPath);
    if ($checkMode) {
        exit(0);
    }

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

    if ($messageCheckMode) {
        (new XserverMail\MailParser())->parse($raw, new DateTimeImmutable('2000-01-01T00:00:00+09:00'));
        exit(0);
    }
    (new DeliveryApplication(
        $webhook, $reporter, $logger, $config, $deduplicator,
        $authenticator, $healthMonitor,
    ))->deliver($raw);
} catch (Throwable $error) {
    if (!isset($reporter)) {
        $exitCode = 1;
    } else {
        try {
            $reporter->report($error, hash('sha256', 'startup'));
        } catch (Throwable) {
            // Reporting must not block inbound mail delivery.
        }
        $exitCode = ($checkMode || $messageCheckMode) ? 1 : 0;
    }
}

exit($exitCode);
