<?php

declare(strict_types=1);

namespace XserverMail;

use Throwable;

final class ErrorReporter
{
    public function __construct(
        private readonly WebhookClient $webhook,
        private readonly OperationalLogger $logger,
        private readonly ?DeliveryHealthMonitor $healthMonitor = null,
    ) {
    }

    public function report(Throwable $error, string $messageIdHash, bool $forceWebhookFailure = false): void
    {
        $classification = self::classify($error);
        if ($this->healthMonitor !== null) {
            if ($forceWebhookFailure) {
                $sequence = $this->healthMonitor->reserveSyntheticFailure();
                if ($sequence !== null) {
                    $this->healthMonitor->recordFailure(
                        $sequence, 'forced_test_failure', $messageIdHash,
                    );
                }
                $this->safeLog('failure', $messageIdHash, 'forced_test_failure', null);
                return;
            }
            $observed = $this->webhook->sendObserved(
                'メール通知システムエラー',
                '処理に失敗しました。分類: ' . $classification,
            );
            $result = $observed->result;
            if ($observed->sequence !== null) {
                if ($result->isSuccess()) {
                    $this->healthMonitor->recordSuccess($observed->sequence);
                } else {
                    $this->healthMonitor->recordFailure(
                        $observed->sequence, $result->classification, $messageIdHash,
                    );
                }
            }
            $this->safeLog(
                $result->isSuccess() ? 'success' : 'failure',
                $messageIdHash,
                $result->isSuccess() ? $classification : $result->classification,
                $result->httpStatus,
            );
            return;
        }

        $result = $forceWebhookFailure
            ? new WebhookResult(false, null, 'forced_test_failure')
            : $this->webhook->send('メール通知システムエラー', '処理に失敗しました。分類: ' . $classification);
        if ($result->isSuccess()) {
            $this->safeLog('success', $messageIdHash, $classification, $result->httpStatus);
            return;
        }

        $this->safeLog('failure', $messageIdHash, $result->classification, $result->httpStatus);
    }

    private function safeLog(string $outcome, string $hash, string $classification, ?int $status): void
    {
        try {
            $this->logger->log($outcome, $hash, $classification, $status);
        } catch (Throwable) {
            // Reporting must never break inbound mail delivery.
        }
    }

    private static function classify(Throwable $error): string
    {
        return 'internal_error';
    }
}
