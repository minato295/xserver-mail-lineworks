<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use JsonException;
use Throwable;

final class WebhookResult
{
    public function __construct(
        private readonly bool $success,
        public readonly ?int $httpStatus,
        public readonly string $classification,
    ) {
    }

    public function isSuccess(): bool
    {
        return $this->success;
    }
}

final class ObservedWebhookResult
{
    public function __construct(
        public readonly WebhookResult $result,
        public readonly ?int $sequence,
    ) {
    }
}

final class WebhookClient
{
    private readonly Closure $transport;
    private readonly Closure $sleeper;

    public function __construct(
        private readonly string $webhookUrl,
        ?callable $transport = null,
        private readonly int $softCapBytes = 32_768,
        ?callable $sleeper = null,
        private readonly ?DeliveryHealthMonitor $healthMonitor = null,
    ) {
        if ($softCapBytes < 32) {
            throw new \InvalidArgumentException('Webhook soft cap is too small');
        }
        $this->transport = Closure::fromCallable($transport ?? self::defaultTransport(...));
        $this->sleeper = Closure::fromCallable($sleeper ?? static fn (int $seconds): int => sleep($seconds));
    }

    public function send(string $title, string $text): WebhookResult
    {
        return $this->sendObserved($title, $text)->result;
    }

    public function sendObserved(string $title, string $text): ObservedWebhookResult
    {
        try {
            $payload = $this->payload($title, $text);
        } catch (JsonException) {
            return new ObservedWebhookResult(
                new WebhookResult(false, null, 'invalid_payload'),
                $this->healthMonitor?->reserveSyntheticFailure(),
            );
        }

        $sequence = $this->healthMonitor?->reserveObservation();
        $result = $this->requestWithRateLimitRetry($payload);
        if ($result->isSuccess()) {
            return new ObservedWebhookResult($result, $sequence);
        }

        if ($result->httpStatus !== 400
            || $result->classification !== 'invalid_parameter'
            || strlen($payload) <= $this->softCapBytes) {
            return new ObservedWebhookResult($result, $sequence);
        }

        $chunks = $this->splitText($text);
        $count = count($chunks);
        foreach ($chunks as $index => $chunk) {
            try {
                $chunkPayload = $this->payload($title, sprintf('(%d/%d) %s', $index + 1, $count, $chunk));
            } catch (JsonException) {
                return new ObservedWebhookResult(
                    new WebhookResult(false, null, 'invalid_payload'),
                    $sequence,
                );
            }
            $chunkResult = $this->requestWithRateLimitRetry($chunkPayload);
            if (!$chunkResult->isSuccess()) {
                return new ObservedWebhookResult($chunkResult, $sequence);
            }
        }

        return new ObservedWebhookResult(new WebhookResult(true, 200, 'success'), $sequence);
    }

    private function payload(string $title, string $text): string
    {
        if ($text === '' || preg_match('//u', $title . $text) !== 1) {
            throw new JsonException('Invalid webhook value');
        }

        return json_encode(
            ['title' => $title, 'body' => ['text' => $text]],
            JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES,
        );
    }

    private function requestWithRateLimitRetry(string $payload): WebhookResult
    {
        $response = $this->request($payload);
        if ($response['result']->httpStatus !== 429) {
            return $response['result'];
        }

        $reset = filter_var($response['headers']['ratelimit-reset'] ?? null, FILTER_VALIDATE_INT);
        if ($reset === false || $reset < 0 || $reset > 15) {
            return $response['result'];
        }
        ($this->sleeper)($reset);

        return $this->request($payload)['result'];
    }

    /** @return array{result:WebhookResult,headers:array<string,string>} */
    private function request(string $payload): array
    {
        try {
            /** @var array{status:int,body:string,headers?:array<string,string>} $response */
            $response = ($this->transport)($this->webhookUrl, $payload, 5, 15);
            $status = $response['status'];
            $body = json_decode($response['body'], true, 16, JSON_THROW_ON_ERROR);
            $description = is_array($body) && is_string($body['description'] ?? null)
                ? trim($body['description'])
                : '';
            $code = is_array($body) ? ($body['code'] ?? null) : null;
            $success = $status === 200 && $code === 200 && $description === 'success';
            $classification = $success ? 'success' : match ($description) {
                'invalid parameter' => 'invalid_parameter',
                'missing parameter' => 'missing_parameter',
                'invalid webhook URL' => 'invalid_webhook_url',
                'too many request' => 'rate_limited',
                default => 'http_error',
            };
            $headers = [];
            foreach (($response['headers'] ?? []) as $name => $value) {
                $headers[strtolower($name)] = $value;
            }

            return ['result' => new WebhookResult($success, $status, $classification), 'headers' => $headers];
        } catch (Throwable) {
            return ['result' => new WebhookResult(false, null, 'transport_error'), 'headers' => []];
        }
    }

    /** @return list<string> */
    private function splitText(string $text): array
    {
        $paragraphs = preg_split('/\n{2,}|\n/u', $text, -1, PREG_SPLIT_NO_EMPTY) ?: [$text];
        $target = max(1, intdiv($this->softCapBytes, 2));
        $chunks = [];
        foreach ($paragraphs as $paragraph) {
            while (strlen($paragraph) > $target) {
                $cut = $target;
                while ($cut > 0 && (ord($paragraph[$cut]) & 0xC0) === 0x80) {
                    --$cut;
                }
                $chunks[] = substr($paragraph, 0, $cut);
                $paragraph = substr($paragraph, $cut);
            }
            if ($paragraph !== '') {
                $chunks[] = $paragraph;
            }
        }

        return $chunks;
    }

    /** @return array{status:int,body:string,headers:array<string,string>} */
    private static function defaultTransport(string $url, string $payload, int $connectTimeout, int $timeout): array
    {
        if (!function_exists('curl_init')) {
            throw new \RuntimeException('HTTP transport unavailable');
        }
        $headers = [];
        $handle = curl_init($url);
        if ($handle === false) {
            throw new \RuntimeException('HTTP transport unavailable');
        }
        curl_setopt_array($handle, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $payload,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => $connectTimeout,
            CURLOPT_TIMEOUT => $timeout,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_HEADERFUNCTION => static function ($curl, string $line) use (&$headers): int {
                $parts = explode(':', $line, 2);
                if (count($parts) === 2) {
                    $headers[trim($parts[0])] = trim($parts[1]);
                }
                return strlen($line);
            },
        ]);
        $body = curl_exec($handle);
        if (!is_string($body)) {
            throw new \RuntimeException('HTTP request failed');
        }
        $status = (int) curl_getinfo($handle, CURLINFO_RESPONSE_CODE);

        return ['status' => $status, 'body' => $body, 'headers' => $headers];
    }
}
