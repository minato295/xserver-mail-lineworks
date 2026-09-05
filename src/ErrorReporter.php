<?php

declare(strict_types=1);

namespace XserverMail;

use Throwable;

final class ErrorReporter
{
    public function __construct(
        WebhookClient $webhook, // Backwards-compatible argument; reporting never uses LINE WORKS.
        private readonly OperationalLogger $logger,
        private readonly ?DeliveryHealthMonitor $healthMonitor = null,
    ) {
    }

    public function report(
        Throwable $error,
        string $messageIdHash,
        bool $forceWebhookFailure = false,
        string $stage = 'delivery',
        ?int $sequence = null,
    ): void {
        $classification = $forceWebhookFailure ? 'forced_test_failure' : 'internal_error';
        $sequence ??= $this->healthMonitor?->reserveObservation();
        if ($sequence !== null) {
            $this->healthMonitor?->recordFailure($sequence, $classification, $messageIdHash);
        }
        try {
            $this->logger->logException($error, $messageIdHash, $stage, $classification);
        } catch (Throwable) {
            // Reporting must never break inbound mail delivery.
        }
    }

}
