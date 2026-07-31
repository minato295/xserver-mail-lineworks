<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use JsonException;
use Throwable;

final class WebhookAttemptDiagnostic
{
    public function __construct(
        public readonly ?int $httpStatus,
        public readonly int|string|null $providerCode,
        public readonly ?string $providerDescription,
        public readonly string $responseFormat,
        public readonly ?string $responseContentType,
        public readonly int $responseBodyBytes,
        public readonly ?string $responseBodySha256,
    ) {
    }
}

final class WebhookDiagnostic
{
    /** @param list<WebhookAttemptDiagnostic> $attempts */
    public function __construct(
        public readonly array $attempts,
        public readonly int $payloadBytes,
        public readonly int $titleCharacters,
        public readonly int $textCharacters,
        public readonly bool $recoveredByRetry,
    ) {
    }

    /** @return list<?int> */
    public function attemptHttpStatuses(): array
    {
        return array_map(static fn (WebhookAttemptDiagnostic $item): ?int => $item->httpStatus, $this->attempts);
    }
}

final class WebhookResult
{
    public function __construct(
        private readonly bool $success,
        public readonly ?int $httpStatus,
        public readonly string $classification,
        public readonly ?WebhookDiagnostic $diagnostic = null,
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
        $request = $this->requestWithRateLimitRetry($payload);
        $result = $this->withDiagnostic($request, $payload, $title, $text);
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
        $attempts = $request['attempts'];
        $recoveredByRetry = $request['recoveredByRetry'];
        foreach ($chunks as $index => $chunk) {
            try {
                $chunkPayload = $this->payload($title, sprintf('(%d/%d) %s', $index + 1, $count, $chunk));
            } catch (JsonException) {
                return new ObservedWebhookResult(
                    new WebhookResult(false, null, 'invalid_payload'),
                    $sequence,
                );
            }
            $chunkRequest = $this->requestWithRateLimitRetry($chunkPayload);
            $attempts = [...$attempts, ...$chunkRequest['attempts']];
            $recoveredByRetry = $recoveredByRetry || $chunkRequest['recoveredByRetry'];
            $chunkResult = $this->withDiagnostic(
                ['result' => $chunkRequest['result'], 'attempts' => $attempts, 'recoveredByRetry' => $recoveredByRetry],
                $payload,
                $title,
                $text,
            );
            if (!$chunkResult->isSuccess()) {
                return new ObservedWebhookResult($chunkResult, $sequence);
            }
        }

        return new ObservedWebhookResult(
            new WebhookResult(
                true,
                200,
                'success',
                $this->diagnostic($attempts, $payload, $title, $text, $recoveredByRetry),
            ),
            $sequence,
        );
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

    /**
     * @return array{result:WebhookResult,attempts:list<WebhookAttemptDiagnostic>,recoveredByRetry:bool}
     */
    private function requestWithRateLimitRetry(string $payload): array
    {
        $response = $this->request($payload);
        $attempts = [$response['attempt']];
        $status = $response['result']->httpStatus;
        $delay = null;
        if ($status !== null && $status >= 500 && $status <= 599) {
            $delay = 5;
        } elseif ($status === 429) {
            $reset = filter_var($response['headers']['ratelimit-reset'] ?? null, FILTER_VALIDATE_INT);
            if ($reset !== false && $reset >= 0 && $reset <= 15) {
                $delay = $reset;
            }
        }
        if ($delay === null) {
            return ['result' => $response['result'], 'attempts' => $attempts, 'recoveredByRetry' => false];
        }
        ($this->sleeper)($delay);

        $retry = $this->request($payload);
        $attempts[] = $retry['attempt'];
        return [
            'result' => $retry['result'],
            'attempts' => $attempts,
            'recoveredByRetry' => $retry['result']->isSuccess(),
        ];
    }

    /** @return array{result:WebhookResult,headers:array<string,string>,attempt:WebhookAttemptDiagnostic} */
    private function request(string $payload): array
    {
        try {
            /** @var array{status:int,body:string,headers?:array<string,string>} $response */
            $response = ($this->transport)($this->webhookUrl, $payload, 5, 15);
            $status = $response['status'];
            $rawBody = $response['body'];
            $headers = $this->normalizedHeaders($response['headers'] ?? []);
            $contentType = $this->safeValue($headers['content-type'] ?? null, 100);
            $bodyBytes = strlen($rawBody);
            $bodyHash = hash('sha256', $rawBody);
            try {
                $body = json_decode($rawBody, true, 16, JSON_THROW_ON_ERROR);
            } catch (JsonException) {
                return [
                    'result' => new WebhookResult(false, $status, 'http_error'),
                    'headers' => $headers,
                    'attempt' => new WebhookAttemptDiagnostic(
                        $status, null, null, 'invalid_json', $contentType, $bodyBytes, $bodyHash,
                    ),
                ];
            }
            $description = is_array($body) && is_string($body['description'] ?? null)
                ? $this->safeValue($body['description'], 200)
                : '';
            $code = is_array($body) && (is_int($body['code'] ?? null) || is_string($body['code'] ?? null))
                ? $body['code']
                : null;
            if (is_string($code)) {
                $code = $this->safeValue($code, 64);
            }
            $success = $status === 200 && $code === 200 && $description === 'success';
            $classification = $success ? 'success' : match ($description) {
                'invalid parameter' => 'invalid_parameter',
                'missing parameter' => 'missing_parameter',
                'invalid webhook URL' => 'invalid_webhook_url',
                'too many request' => 'rate_limited',
                default => 'http_error',
            };
            $diagnosticDescription = $description;
            if ($description !== '' && $this->isPayloadEcho($description, $payload)) {
                $diagnosticDescription = '';
            }

            return [
                'result' => new WebhookResult($success, $status, $classification),
                'headers' => $headers,
                'attempt' => new WebhookAttemptDiagnostic(
                    $status, $code, $diagnosticDescription === '' ? null : $diagnosticDescription,
                    'json', $contentType, $bodyBytes, $bodyHash,
                ),
            ];
        } catch (Throwable) {
            return [
                'result' => new WebhookResult(false, null, 'transport_error'),
                'headers' => [],
                'attempt' => new WebhookAttemptDiagnostic(null, null, null, 'transport_error', null, 0, null),
            ];
        }
    }

    /** @param array{result:WebhookResult,attempts:list<WebhookAttemptDiagnostic>,recoveredByRetry:bool} $request */
    private function withDiagnostic(array $request, string $payload, string $title, string $text): WebhookResult
    {
        $result = $request['result'];
        return new WebhookResult(
            $result->isSuccess(),
            $result->httpStatus,
            $result->classification,
            $this->diagnostic($request['attempts'], $payload, $title, $text, $request['recoveredByRetry']),
        );
    }

    /** @param list<WebhookAttemptDiagnostic> $attempts */
    private function diagnostic(array $attempts, string $payload, string $title, string $text, bool $recoveredByRetry): WebhookDiagnostic
    {
        return new WebhookDiagnostic(
            $attempts,
            strlen($payload),
            $this->characterCount($title),
            $this->characterCount($text),
            $recoveredByRetry,
        );
    }

    /** @param array<array-key,mixed> $responseHeaders @return array<string,string> */
    private function normalizedHeaders(array $responseHeaders): array
    {
        $headers = [];
        foreach ($responseHeaders as $name => $value) {
            if (is_string($name) && is_string($value)) {
                $headers[strtolower($name)] = $value;
            }
        }
        return $headers;
    }

    private function safeValue(?string $value, int $maximumCharacters): ?string
    {
        if ($value === null) {
            return null;
        }
        $safe = trim((string) preg_replace('/[\\x00-\\x1F\\x7F-\\x9F]/u', '', $value));
        $characters = preg_split('//u', $safe, -1, PREG_SPLIT_NO_EMPTY) ?: [];
        return implode('', array_slice($characters, 0, $maximumCharacters));
    }

    private function characterCount(string $value): int
    {
        return count(preg_split('//u', $value, -1, PREG_SPLIT_NO_EMPTY) ?: []);
    }

    private function isPayloadEcho(string $description, string $payload): bool
    {
        if ($this->characterCount($description) < 8) {
            return false;
        }
        try {
            $decoded = json_decode($payload, true, 8, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            return true;
        }
        $title = is_array($decoded) && is_string($decoded['title'] ?? null) ? $decoded['title'] : '';
        $text = is_array($decoded) && is_array($decoded['body'] ?? null)
            && is_string($decoded['body']['text'] ?? null) ? $decoded['body']['text'] : '';
        return str_contains($title, $description) || str_contains($text, $description);
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
