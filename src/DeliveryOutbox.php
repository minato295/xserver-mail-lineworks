<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use RuntimeException;

final class DeliveryOutboxConflict extends RuntimeException
{
}

/** Conservative recovery: only a definitely rejected entire operation can be replayed. */
final class DeliveryOutbox
{
    private const MAX_ITEMS = 100;
    private const MAX_ITEM_BYTES = 1048576;
    private const MAX_BYTES = 16777216;
    private const RETENTION = 604800;
    private readonly Closure $clock;
    private readonly PrivateStateFilesystem $filesystem;

    public function __construct(
        private readonly string $path,
        ?PrivateStateFilesystem $filesystem = null,
        ?callable $clock = null,
    ) {
        $this->filesystem = $filesystem ?? new NativePrivateStateFilesystem();
        $this->clock = Closure::fromCallable($clock ?? static fn (): int => time());
    }

    /** @param list<string> $recipients */
    public function begin(string $id, array $recipients, string $title, string $text): ?string
    {
        $this->hash($id);
        $payload = ['recipients' => CanonicalEmail::many($recipients, false), 'title' => $title, 'text' => $text];
        if ($text === '' || strlen($this->encode($payload)) > self::MAX_ITEM_BYTES - 2048) {
            $this->fail();
        }
        $digest = hash('sha256', $this->encode($payload));
        return $this->mutate(function (array &$items, int $now) use ($id, $payload, $digest): ?string {
            if (isset($items[$id])) {
                if (!hash_equals($items[$id]['digest'], $digest)) {
                    throw new DeliveryOutboxConflict('Private recovery conflict');
                }
                return null;
            }
            if (count($items) >= self::MAX_ITEMS) {
                // Keep pending/review work; successful tombstones are best-effort duplicate protection.
                $delivered = array_filter($items, static fn (array $item): bool => $item['state'] === 'delivered');
                uasort($delivered, static fn (array $a, array $b): int => $a['created_at'] <=> $b['created_at']);
                if ($delivered === []) {
                    $this->fail();
                }
                unset($items[array_key_first($delivered)]);
            }
            $token = bin2hex(random_bytes(32));
            $items[$id] = [
                'id' => $id, 'revision' => bin2hex(random_bytes(32)), 'state' => 'in_flight',
                'created_at' => $now, 'expires_at' => $now + self::RETENTION,
                'digest' => $digest, 'token' => $token, 'payload' => $payload,
            ];
            return $token;
        });
    }

    public function finish(string $id, string $token, WebhookResult $result): void
    {
        $this->hash($id);
        $this->hash($token);
        $safe = !$result->isSuccess() && $this->isDefiniteTotalRejection($result);
        $this->mutate(function (array &$items) use ($id, $token, $result, $safe): void {
            $item = $items[$id] ?? null;
            if (!is_array($item) || $item['state'] !== 'in_flight' || !hash_equals($item['token'], $token)) {
                $this->fail();
            }
            $item['state'] = $result->isSuccess() ? 'delivered' : ($safe ? 'pending' : 'review_only');
            $item['revision'] = bin2hex(random_bytes(32));
            $item['token'] = null;
            if ($result->isSuccess()) {
                $item['payload'] = null;
            }
            $items[$id] = $item;
        });
    }

    /** Missing, expired, already-claimed, stale and review-only rows all refuse before transport. */
    public function claimSelected(string $id, string $revision, array $currentTargets): array
    {
        $this->hash($id);
        $this->hash($revision);
        $targets = array_map('strtolower', CanonicalEmail::many($currentTargets, true));
        return $this->mutate(function (array &$items) use ($id, $revision, $targets): array {
            $item = $items[$id] ?? null;
            if (!is_array($item) || $item['state'] !== 'pending' || !hash_equals($item['revision'], $revision)
                || array_intersect(array_map('strtolower', $item['payload']['recipients']), $targets) === []) {
                $this->fail();
            }
            $item['state'] = 'in_flight';
            $item['token'] = bin2hex(random_bytes(32));
            $item['revision'] = bin2hex(random_bytes(32));
            $items[$id] = $item;
            return ['token' => $item['token'], 'title' => $item['payload']['title'], 'text' => $item['payload']['text']];
        });
    }

    /** No payload, recipients, provider text, webhook URL or notification side effects. */
    public function listMetadata(): array
    {
        return $this->filesystem->withExclusiveLock(dirname($this->path) . '/.delivery-outbox.lock', function (): array {
            $now = $this->now();
            $rows = [];
            foreach ($this->read() as $item) {
                $expired = $item['expires_at'] <= $now;
                $rows[] = [
                    'id' => $item['id'], 'revision' => $item['revision'],
                    'state' => $expired ? 'expired' : $item['state'],
                    'created_at' => $item['created_at'], 'expires_at' => $item['expires_at'],
                    'completed_chunks' => $item['state'] === 'pending' ? 0 : null,
                    'total_chunks' => null,
                    'classification' => $item['state'] === 'pending' ? 'definite_rejection' :
                        ($item['state'] === 'delivered' ? 'success' : 'review_required'),
                    'retryable' => !$expired && $item['state'] === 'pending',
                ];
            }
            return ['schema_version' => 1, 'items' => $rows];
        });
    }

    private function isDefiniteTotalRejection(WebhookResult $result): bool
    {
        $attempts = $result->diagnostic?->attempts ?? [];
        if ($attempts === []) {
            return false;
        }
        foreach ($attempts as $attempt) {
            if (!$attempt instanceof WebhookAttemptDiagnostic || $attempt->responseFormat !== 'json') {
                return false;
            }
            $description = $attempt->providerDescription;
            $code = $attempt->providerCode;
            if ($attempt->httpStatus === 400 && ($code === 400 || $code === '400')
                && in_array($description, ['invalid parameter', 'missing parameter', 'invalid webhook URL',
                    'limit exceeded (body.text length exceeds 2000)'], true)) {
                continue;
            }
            if ($attempt->httpStatus === 429 && ($code === 429 || $code === '429') && $description === 'too many request') {
                continue;
            }
            return false;
        }
        return true;
    }

    private function mutate(callable $operation): mixed
    {
        return $this->filesystem->withExclusiveLock(dirname($this->path) . '/.delivery-outbox.lock', function () use ($operation): mixed {
            $items = $this->read();
            $now = $this->now();
            $items = array_filter($items, static fn (array $item): bool => $item['expires_at'] > $now);
            $result = $operation($items, $now);
            $bytes = $this->encode(['schema_version' => 1, 'items' => (object) $items]);
            if (strlen($bytes) > self::MAX_BYTES) {
                $this->fail();
            }
            $this->filesystem->assertExclusiveLockCurrent();
            $this->filesystem->replaceAtomic($this->path, $bytes, 0600);
            return $result;
        });
    }

    private function read(): array
    {
        $bytes = $this->filesystem->readRegular($this->path, self::MAX_BYTES);
        if ($bytes === null) {
            return [];
        }
        try {
            // This store has only this writer. Canonical round-trip rejects duplicate keys at
            // every nesting level (including escaped aliases), instead of last-key-wins parsing.
            $object = json_decode($bytes, false, 16, JSON_THROW_ON_ERROR);
            if (!hash_equals($bytes, $this->encode($object))) {
                $this->fail();
            }
            $value = json_decode($bytes, true, 16, JSON_THROW_ON_ERROR);
        } catch (\Throwable) {
            $this->fail();
        }
        if (!is_array($value) || array_keys($value) !== ['schema_version', 'items'] || $value['schema_version'] !== 1
            || !is_array($value['items']) || count($value['items']) > self::MAX_ITEMS) {
            $this->fail();
        }
        foreach ($value['items'] as $id => $item) {
            $this->hash($id);
            if (!is_array($item) || array_keys($item) !== ['id', 'revision', 'state', 'created_at', 'expires_at', 'digest', 'token', 'payload']
                || $item['id'] !== $id || !in_array($item['state'], ['in_flight', 'pending', 'review_only', 'delivered'], true)
                || !is_int($item['created_at']) || $item['created_at'] < 0 || !is_int($item['expires_at'])
                || $item['expires_at'] !== $item['created_at'] + self::RETENTION
                || strlen($this->encode($item)) > self::MAX_ITEM_BYTES) {
                $this->fail();
            }
            $this->hash($item['revision']);
            $this->hash($item['digest']);
            if ($item['state'] === 'in_flight') {
                $this->hash($item['token']);
            } elseif ($item['token'] !== null) {
                $this->fail();
            }
            if ($item['state'] === 'delivered') {
                if ($item['payload'] !== null) {
                    $this->fail();
                }
                continue;
            }
            $payload = $item['payload'];
            if (!is_array($payload) || array_keys($payload) !== ['recipients', 'title', 'text'] || !is_string($payload['title'])
                || !is_string($payload['text']) || $payload['text'] === ''
                || !hash_equals($item['digest'], hash('sha256', $this->encode($payload)))) {
                $this->fail();
            }
            try {
                if (CanonicalEmail::many($payload['recipients'], false) !== $payload['recipients']) {
                    $this->fail();
                }
            } catch (\Throwable) {
                $this->fail();
            }
        }
        return $value['items'];
    }

    private function now(): int
    {
        $now = ($this->clock)();
        if (!is_int($now) || $now < 0) {
            $this->fail();
        }
        return $now;
    }

    private function hash(mixed $value): void
    {
        if (!is_string($value) || preg_match('/\A[a-f0-9]{64}\z/D', $value) !== 1) {
            $this->fail();
        }
    }

    private function encode(mixed $value): string
    {
        try {
            return json_encode($value, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        } catch (\Throwable) {
            $this->fail();
        }
    }

    private function fail(): never
    {
        throw new RuntimeException('Private recovery unavailable');
    }
}
