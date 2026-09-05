<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use DateTimeImmutable;
use RuntimeException;
use Throwable;

final class DeliveryApplication
{
    private readonly Closure $utcClock;
    private readonly Closure $parser;

    public function __construct(
        private readonly WebhookClient $webhook,
        private readonly ErrorReporter $reporter,
        private readonly OperationalLogger $logger,
        private readonly ?NotifierConfig $config = null,
        private readonly ?DeliveryDeduplicator $deduplicator = null,
        private readonly ?SystemMailAuthenticator $systemMailAuthenticator = null,
        private readonly ?DeliveryHealthMonitor $healthMonitor = null,
        ?callable $utcClock = null,
        ?callable $parser = null,
    ) {
        $this->utcClock = Closure::fromCallable(
            $utcClock ?? static fn (): DateTimeImmutable => new DateTimeImmutable('now'),
        );
        $this->parser = Closure::fromCallable(
            $parser ?? static fn (string $raw, DateTimeImmutable $now): MailMessage =>
                (new MailParser())->parse($raw, $now),
        );
    }

    public function deliver(string $raw): void
    {
        if ($this->systemMailAuthenticator?->isAuthentic($raw) === true) {
            try {
                $this->logger->log(
                    'success', hash('sha256', 'system-mail-suppressed'),
                    'system_mail_suppressed', null,
                );
            } catch (Throwable) {
            }
            return;
        }
        $messageIdHash = hash('sha256', $raw);
        $reservation = null;
        $stage = 'input';
        $sequence = null;
        try {
            if (strlen($raw) > 10 * 1024 * 1024) {
                throw new RuntimeException('Input exceeds limit');
            }
            $stage = 'clock';
            $now = ($this->utcClock)();
            if (!$now instanceof DateTimeImmutable) {
                throw new RuntimeException('Invalid delivery clock');
            }
            $stage = 'parse';
            $message = ($this->parser)($raw, $now);
            if (!$message instanceof MailMessage) {
                throw new RuntimeException('Invalid parser result');
            }
            $messageIdHash = $this->deduplicationKey($message, $raw);
            if ($this->config !== null && !$this->isNotificationTarget($message)) {
                try {
                    $this->logger->log('ignored', $messageIdHash, 'non_target_recipient', null);
                } catch (Throwable) {
                    // Recording a normal exclusion must not trigger an incident.
                }
                return;
            }
            if ($this->deduplicator !== null) {
                try {
                    $reservation = $this->deduplicator->reserve($messageIdHash);
                    if ($reservation === null) {
                        return;
                    }
                } catch (Throwable) {
                    try {
                        $this->logger->log('failure', $messageIdHash, 'dedup_store_failure', null);
                    } catch (Throwable) {
                        // Deduplication and logging failures must not drop inbound notifications.
                    }
                }
            }
            if ($this->isForcedErrorTest($message)) {
                $this->commitReservation($messageIdHash, $reservation);
                $sequence = $this->healthMonitor?->reserveSyntheticFailure();
                if ($sequence !== null) {
                    $this->healthMonitor?->recordFailure(
                        $sequence, 'forced_test_failure', $messageIdHash,
                    );
                } elseif ($this->healthMonitor === null) {
                    $this->safeReport(new RuntimeException('Forced webhook test failure'), $messageIdHash, true);
                }
                return;
            }
            $stage = 'format';
            $formatter = new NotificationFormatter();
            $title = $formatter->title($message);
            $text = $formatter->format($message);
            $stage = 'webhook';
            $observed = $this->webhook->sendObservedWithCompatibility(
                $title, $text,
            );
            $sequence = $observed->sequence;
            $result = $observed->result;
            if ($result->isSuccess()) {
                if ($observed->sequence !== null) {
                    $this->healthMonitor?->recordSuccess($observed->sequence);
                }
            } elseif ($observed->sequence !== null) {
                $this->healthMonitor?->recordFailure(
                    $observed->sequence, $result->classification, $messageIdHash,
                );
            }
            $this->commitReservation($messageIdHash, $reservation);
            $stage = 'log';
            $this->logger->log(
                $result->isSuccess() ? 'success' : 'failure',
                $messageIdHash,
                $result->classification,
                $result->httpStatus,
                $result->diagnostic,
            );
        } catch (Throwable $error) {
            $this->commitReservation($messageIdHash, $reservation);
            if ($stage === 'log') {
                try {
                    $this->logger->logException($error, $messageIdHash, $stage);
                } catch (Throwable) {
                }
            } else {
                $this->safeReport($error, $messageIdHash, false, $stage, $sequence);
            }
            return;
        }
    }

    private function deduplicationKey(MailMessage $message, string $raw): string
    {
        if (!hash_equals(hash('sha256', ''), $message->messageIdHash)) {
            return $message->messageIdHash;
        }
        return hash('sha256', $raw);
    }

    private function isNotificationTarget(MailMessage $message): bool
    {
        $targets = array_fill_keys(
            array_map(static fn (string $value): string => strtolower($value), $this->config?->notificationTargets ?? []),
            true,
        );
        foreach ($message->visibleRecipientAddresses as $address) {
            if (isset($targets[strtolower($address)])) {
                return true;
            }
        }
        return false;
    }

    private function commitReservation(string $hash, ?string &$token): void
    {
        if ($this->deduplicator === null || $token === null) {
            return;
        }
        $commitToken = $token;
        $token = null;
        try {
            $this->deduplicator->commit($hash, $commitToken);
        } catch (Throwable) {
            try {
                $this->logger->log('failure', $hash, 'dedup_store_failure', null);
            } catch (Throwable) {
                // An uncertain commit must never release the reservation or create another incident.
            }
        }
    }

    private function safeReport(Throwable $error, string $messageIdHash, bool $forceWebhookFailure = false,
        string $stage = 'delivery', ?int $sequence = null): void
    {
        try {
            $this->reporter->report($error, $messageIdHash, $forceWebhookFailure, $stage, $sequence);
        } catch (Throwable) {
            // Inbound mail delivery must remain fail-open even if reporting fails.
        }
    }

    private function isForcedErrorTest(MailMessage $message): bool
    {
        $until = $this->config?->testForceWebhookFailureUntil;
        $token = $this->config?->testErrorSubjectToken;
        $now = ($this->utcClock)();
        return $until instanceof DateTimeImmutable
            && is_string($token)
            && $now instanceof DateTimeImmutable
            && $until > $now
            && hash_equals('[Error Test ' . $token . ']', $message->subject);
    }
}
