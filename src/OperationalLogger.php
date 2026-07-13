<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use DateTimeImmutable;
use DateTimeZone;
use RuntimeException;

final class OperationalLogger
{
    private const CLASSIFICATIONS = [
        'success', 'invalid_payload', 'invalid_parameter', 'missing_parameter',
        'invalid_webhook_url', 'rate_limited', 'http_error', 'transport_error',
        'forced_test_failure', 'internal_error', 'system_mail_suppressed',
        'health_state_failure', 'unknown', 'dedup_store_failure',
    ];

    private readonly Closure $utcClock;

    public function __construct(private readonly string $path, ?callable $utcClock = null)
    {
        $this->utcClock = Closure::fromCallable(
            $utcClock ?? static fn (): DateTimeImmutable => new DateTimeImmutable('now', new DateTimeZone('UTC')),
        );
    }

    public function log(string $outcome, string $messageIdHash, string $classification, ?int $httpStatus): void
    {
        $now = ($this->utcClock)();
        if (!$now instanceof DateTimeImmutable) {
            throw new RuntimeException('Operational log unavailable');
        }
        $safeClassification = in_array($classification, self::CLASSIFICATIONS, true)
            ? $classification : 'unknown';
        $event = [
            'timestamp' => $now->setTimezone(new DateTimeZone('UTC'))->format(DATE_ATOM),
            'outcome' => $outcome,
            'message_id_hash' => preg_match('/\A[a-f0-9]{64}\z/', $messageIdHash) === 1 ? $messageIdHash : hash('sha256', $messageIdHash),
            'classification' => $safeClassification,
            'http_status' => $httpStatus,
        ];
        $line = json_encode($event, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        if (@file_put_contents($this->path, $line, FILE_APPEND | LOCK_EX) === false) {
            throw new RuntimeException('Operational log unavailable');
        }
    }
}
