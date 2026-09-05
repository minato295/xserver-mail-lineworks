<?php

declare(strict_types=1);

namespace XserverMail;

use Throwable;

final class DeliveryRecovery
{
    public function __construct(
        private readonly DeliveryOutbox $outbox,
        private readonly NotifierConfig $config,
        private readonly WebhookClient $webhook,
        private readonly ?OperationalLogger $logger = null,
        private readonly ?DeliveryHealthMonitor $healthMonitor = null,
    ) {
    }

    public function retrySelected(string $id, string $revision): array
    {
        // Persist the exclusive claim, then release all private filesystem locks before HTTP.
        $claim = $this->outbox->claimSelected($id, $revision, $this->config->notificationTargets);
        $observed = $this->webhook->sendObservedWithCompatibility($claim['title'], $claim['text']);
        $result = $observed->result;
        $storageFailed = false;
        try {
            // Save delivery first. A later health or logging failure cannot make it replayable.
            $this->outbox->finish($id, $claim['token'], $result);
        } catch (Throwable) {
            $this->safeLog($id, 'outbox_store_failure');
            $storageFailed = true;
        }
        try {
            if ($observed->sequence !== null) {
                if ($result->isSuccess()) {
                    $this->healthMonitor?->recordSuccess($observed->sequence);
                } else {
                    $this->healthMonitor?->recordFailure($observed->sequence, $result->classification, $id);
                }
            }
        } catch (Throwable) {
            $this->safeLog($id, 'health_state_failure');
        }
        try {
            $this->logger?->log(
                $result->isSuccess() ? 'success' : 'failure', $id,
                $result->classification, $result->httpStatus, $result->diagnostic,
            );
        } catch (Throwable) {
        }
        if ($storageFailed) {
            // Keep the claim uncertain/non-retryable, but never discard a known
            // transport outcome from health or diagnostic reporting.
            throw new \RuntimeException('Private recovery result unavailable');
        }
        return ['schema_version' => 1, 'id' => $id,
            'status' => $result->isSuccess() ? 'delivered' : 'not_delivered'];
    }

    private function safeLog(string $id, string $classification): void
    {
        try {
            $this->logger?->log('failure', $id, $classification, null);
        } catch (Throwable) {
        }
    }
}
