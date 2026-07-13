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
        try {
            if (strlen($raw) > 10 * 1024 * 1024) {
                throw new RuntimeException('Input exceeds limit');
            }
            $now = ($this->utcClock)();
            if (!$now instanceof DateTimeImmutable) {
                throw new RuntimeException('Invalid delivery clock');
            }
            $message = ($this->parser)($raw, $now);
            if (!$message instanceof MailMessage) {
                throw new RuntimeException('Invalid parser result');
            }
            $messageIdHash = $this->deduplicationKey($message, $raw);
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
                $this->releaseReservation($messageIdHash, $reservation);
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
            $formatter = new NotificationFormatter();
            $observed = $this->webhook->sendObserved(
                $formatter->title($message), $formatter->format($message),
            );
            $result = $observed->result;
            if ($result->isSuccess()) {
                if ($observed->sequence !== null) {
                    $this->healthMonitor?->recordSuccess($observed->sequence);
                }
                $this->commitReservation($messageIdHash, $reservation);
            } else {
                $this->releaseReservation($messageIdHash, $reservation);
            }
            $this->logger->log($result->isSuccess() ? 'success' : 'failure', $messageIdHash, $result->classification, $result->httpStatus);
        } catch (Throwable $error) {
            $this->releaseReservation($messageIdHash, $reservation);
            $this->safeReport($error, $messageIdHash);
            return;
        }
        if (!$result->isSuccess()) {
            $this->safeReport(new RuntimeException('Webhook delivery failed'), $messageIdHash);
        }
    }

    private function deduplicationKey(MailMessage $message, string $raw): string
    {
        if (!hash_equals(hash('sha256', ''), $message->messageIdHash)) {
            return $message->messageIdHash;
        }
        return hash('sha256', $raw);
    }

    private function commitReservation(string $hash, ?string &$token): void
    {
        if ($this->deduplicator !== null && $token !== null) {
            $this->deduplicator->commit($hash, $token);
            $token = null;
        }
    }

    private function releaseReservation(string $hash, ?string &$token): void
    {
        if ($this->deduplicator === null || $token === null) {
            return;
        }
        $releaseToken = $token;
        $token = null;
        try {
            $this->deduplicator->release($hash, $releaseToken);
        } catch (Throwable) {
            try { $this->logger->log('failure', $hash, 'dedup_store_failure', null); } catch (Throwable) {}
        }
    }

    private function safeReport(Throwable $error, string $messageIdHash, bool $forceWebhookFailure = false): void
    {
        try {
            $this->reporter->report($error, $messageIdHash, $forceWebhookFailure);
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
