<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use DateTimeImmutable;
use DateTimeZone;
use JsonException;
use RuntimeException;
use Throwable;

final class DeliveryHealthMonitor
{
    private const MAX_STATE_BYTES = 4096;
    private const CLASSIFICATIONS = [
        'success', 'invalid_payload', 'invalid_parameter', 'missing_parameter',
        'invalid_webhook_url', 'rate_limited', 'http_error', 'transport_error',
        'forced_test_failure', 'internal_error', 'system_mail_suppressed',
        'health_state_failure', 'unknown',
    ];

    private readonly Closure $utcClock;
    private readonly Closure $eventId;
    private readonly string $lockPath;

    /** @param list<string> $recipients */
    public function __construct(
        private readonly string $statePath,
        private readonly array $recipients,
        private readonly string $logPath,
        private readonly SystemMailAuthenticator $authenticator,
        private readonly SendmailClient $sendmail,
        private readonly OperationalLogger $logger,
        private readonly PrivateStateFilesystem $filesystem,
        callable $utcClock,
        callable $eventId,
    ) {
        NotifierConfig::assertPrivatePath($statePath);
        NotifierConfig::assertPrivatePath($logPath);
        if (basename($statePath) !== 'delivery-health.json' || dirname($statePath) !== dirname($logPath)) {
            throw new \InvalidArgumentException('Invalid health configuration');
        }
        $this->lockPath = dirname($statePath) . '/.delivery-health.lock';
        $this->utcClock = Closure::fromCallable($utcClock);
        $this->eventId = Closure::fromCallable($eventId);
    }

    public function reserveObservation(): ?int
    {
        try {
            return $this->filesystem->withExclusiveLock($this->lockPath, function (): int {
                $state = $this->readState();
                if ($state['next_observation_sequence'] === PHP_INT_MAX) {
                    throw new RuntimeException('Health sequence exhausted');
                }
                ++$state['next_observation_sequence'];
                $this->writeState($state);
                return $state['next_observation_sequence'];
            });
        } catch (Throwable) {
            $this->logStateFailure();
            return null;
        }
    }

    public function reserveSyntheticFailure(): ?int
    {
        return $this->reserveObservation();
    }

    public function recordSuccess(int $sequence): void
    {
        $this->record($sequence, true, 'success', null);
    }

    public function recordFailure(int $sequence, string $classification, string $hash): void
    {
        $safeClassification = in_array($classification, self::CLASSIFICATIONS, true)
            ? $classification : 'unknown';
        $safeHash = preg_match('/\A[a-f0-9]{64}\z/D', $hash) === 1 ? $hash : hash('sha256', $hash);
        $this->record($sequence, false, $safeClassification, $safeHash);
    }

    public function status(): ?string
    {
        try {
            return $this->filesystem->withExclusiveLock(
                $this->lockPath,
                fn (): string => $this->readState()['status'],
            );
        } catch (Throwable) {
            $this->logStateFailure();
            return null;
        }
    }

    private function record(int $sequence, bool $success, string $classification, ?string $hash): void
    {
        try {
            $this->filesystem->withExclusiveLock($this->lockPath, function () use (
                $sequence, $success, $classification, $hash,
            ): void {
                $state = $this->readState();
                if ($sequence < 1 || $sequence > $state['next_observation_sequence']) {
                    throw new RuntimeException('Invalid health sequence');
                }
                if ($sequence <= $state['last_applied_sequence']) {
                    return;
                }

                $transition = ($state['status'] === 'healthy' && !$success)
                    || ($state['status'] === 'degraded' && $success);
                if (!$transition) {
                    $state['last_applied_sequence'] = $sequence;
                    $this->writeState($state);
                    return;
                }

                $now = $this->now();
                if ($state['status'] === 'healthy') {
                    $mailType = 'error';
                    $mailClassification = $classification;
                    $mailHash = $hash;
                } else {
                    $mailType = 'recovery';
                    $mailClassification = $state['classification'];
                    $mailHash = $state['message_id_hash'];
                }
                if (!is_string($mailHash)) {
                    throw new RuntimeException('Invalid health state');
                }
                try {
                    $this->filesystem->assertExclusiveLockCurrent();
                    $this->sendTransition($mailType, $now, $mailClassification, $mailHash);
                } catch (Throwable) {
                    $state['last_applied_sequence'] = $sequence;
                    $this->writeState($state);
                    return;
                }

                $state['status'] = $success ? 'healthy' : 'degraded';
                $state['changed_at'] = $now->format('Y-m-d\TH:i:s\Z');
                $state['last_applied_sequence'] = $sequence;
                if ($success) {
                    unset($state['classification'], $state['message_id_hash']);
                } else {
                    $state['classification'] = $classification;
                    $state['message_id_hash'] = $hash;
                }
                $this->writeState($state);
            });
        } catch (Throwable) {
            $this->logStateFailure();
        }
    }

    private function sendTransition(
        string $type,
        DateTimeImmutable $now,
        string $classification,
        string $hash,
    ): void {
        $event = ($this->eventId)();
        if (!is_string($event) || preg_match('/\A[a-f0-9]{32}\z/D', $event) !== 1) {
            throw new RuntimeException('Invalid health event');
        }
        $body = 'UTC time: ' . $now->format('Y-m-d\TH:i:s\Z') . "\n"
            . 'Classification: ' . $classification . "\n"
            . 'Message-ID hash: ' . $hash . "\n"
            . 'Operational log: ' . $this->logPath . "\n";
        $wire = $this->authenticator->build(
            $type,
            $this->recipients,
            $now->format('D, d M Y H:i:s +0000'),
            $event,
            $body,
        );
        $this->sendmail->send($wire, 15);
    }

    /** @return array<string,mixed> */
    private function readState(): array
    {
        $bytes = $this->filesystem->readRegular($this->statePath, self::MAX_STATE_BYTES);
        if ($bytes === null) {
            $state = [
                'schema_version' => 1,
                'status' => 'healthy',
                'changed_at' => $this->now()->format('Y-m-d\TH:i:s\Z'),
                'next_observation_sequence' => 0,
                'last_applied_sequence' => 0,
            ];
            $this->writeState($state);
            return $state;
        }
        $this->assertNoDuplicateKeys($bytes);
        try {
            $state = json_decode($bytes, true, 16, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            throw new RuntimeException('Invalid health state');
        }
        if (!is_array($state) || array_is_list($state)) {
            throw new RuntimeException('Invalid health state');
        }
        $status = $state['status'] ?? null;
        $expected = $status === 'degraded'
            ? ['schema_version', 'status', 'changed_at', 'next_observation_sequence',
                'last_applied_sequence', 'classification', 'message_id_hash']
            : ['schema_version', 'status', 'changed_at', 'next_observation_sequence',
                'last_applied_sequence'];
        $actualKeys = array_keys($state);
        sort($actualKeys, SORT_STRING);
        sort($expected, SORT_STRING);
        if ($actualKeys !== $expected || ($state['schema_version'] ?? null) !== 1
            || !in_array($status, ['healthy', 'degraded'], true)
            || !$this->validTimestamp($state['changed_at'] ?? null)
            || !is_int($state['next_observation_sequence'] ?? null)
            || $state['next_observation_sequence'] < 0
            || !is_int($state['last_applied_sequence'] ?? null)
            || $state['last_applied_sequence'] < 0
            || $state['last_applied_sequence'] > $state['next_observation_sequence']
            || ($status === 'degraded' && (!is_string($state['classification'] ?? null)
                || !in_array($state['classification'], self::CLASSIFICATIONS, true)
                || $state['classification'] === 'success'
                || preg_match('/\A[a-f0-9]{64}\z/D', $state['message_id_hash'] ?? '') !== 1))) {
            throw new RuntimeException('Invalid health state');
        }
        return $state;
    }

    /** @param array<string,mixed> $state */
    private function writeState(array $state): void
    {
        try {
            $bytes = json_encode($state, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        } catch (JsonException) {
            throw new RuntimeException('Private state unavailable');
        }
        if (strlen($bytes) > self::MAX_STATE_BYTES) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->filesystem->replaceAtomic($this->statePath, $bytes, 0600);
    }

    private function now(): DateTimeImmutable
    {
        $now = ($this->utcClock)();
        if (!$now instanceof DateTimeImmutable) {
            throw new RuntimeException('Invalid health clock');
        }
        return $now->setTimezone(new DateTimeZone('UTC'));
    }

    private function validTimestamp(mixed $value): bool
    {
        if (!is_string($value)) {
            return false;
        }
        $parsed = DateTimeImmutable::createFromFormat('!Y-m-d\TH:i:s\Z', $value, new DateTimeZone('UTC'));
        return $parsed instanceof DateTimeImmutable && $parsed->format('Y-m-d\TH:i:s\Z') === $value;
    }

    private function logStateFailure(): void
    {
        try {
            $this->logger->log('failure', hash('sha256', 'delivery-health-state'), 'health_state_failure', null);
        } catch (Throwable) {
        }
    }

    private function assertNoDuplicateKeys(string $bytes): void
    {
        $length = strlen($bytes);
        $offset = 0;
        $skip = static function () use ($bytes, $length, &$offset): void {
            while ($offset < $length && str_contains(" \t\r\n", $bytes[$offset])) { ++$offset; }
        };
        $string = static function () use ($bytes, $length, &$offset): string {
            if ($offset >= $length || $bytes[$offset] !== '"') { throw new RuntimeException(); }
            $start = $offset++;
            while ($offset < $length) {
                if ($bytes[$offset] === '\\') { $offset += 2; continue; }
                if ($bytes[$offset++] === '"') {
                    $decoded = json_decode(substr($bytes, $start, $offset - $start), false, 2, JSON_THROW_ON_ERROR);
                    if (!is_string($decoded)) { throw new RuntimeException(); }
                    return $decoded;
                }
            }
            throw new RuntimeException();
        };
        $skip();
        if ($offset >= $length || $bytes[$offset++] !== '{') { throw new RuntimeException(); }
        $keys = [];
        while (true) {
            $skip();
            if ($offset < $length && $bytes[$offset] === '}') { ++$offset; break; }
            $key = $string();
            if (isset($keys['k:' . $key])) { throw new RuntimeException(); }
            $keys['k:' . $key] = true;
            $skip();
            if ($offset >= $length || $bytes[$offset++] !== ':') { throw new RuntimeException(); }
            $skip();
            $depth = 0; $inside = false; $escaped = false;
            while ($offset < $length) {
                $character = $bytes[$offset];
                if ($inside) {
                    ++$offset;
                    if ($escaped) { $escaped = false; }
                    elseif ($character === '\\') { $escaped = true; }
                    elseif ($character === '"') { $inside = false; }
                } elseif ($character === '"') { $inside = true; ++$offset; }
                elseif ($character === '{' || $character === '[') { ++$depth; ++$offset; }
                elseif ($character === '}' || $character === ']') {
                    if ($depth === 0) { break; }
                    --$depth; ++$offset;
                } elseif ($character === ',' && $depth === 0) { break; }
                else { ++$offset; }
            }
            $skip();
            if ($offset < $length && $bytes[$offset] === ',') { ++$offset; continue; }
            if ($offset < $length && $bytes[$offset] === '}') { ++$offset; break; }
            throw new RuntimeException();
        }
        $skip();
        if ($offset !== $length) { throw new RuntimeException(); }
    }
}
