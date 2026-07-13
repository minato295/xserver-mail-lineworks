<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeImmutable;
use InvalidArgumentException;
use JsonException;
use RuntimeException;

final class DeliveryDeduplicator
{
    private const MAX_STATE_BYTES = 1_048_576;
    private const LOCK_BASENAME = '.delivery-dedup.lock';

    private readonly string $path;

    public function __construct(string $path, private readonly int $ttlSeconds = 600)
    {
        NotifierConfig::assertPrivatePath($path);
        if ($ttlSeconds < 1 || basename($path) === self::LOCK_BASENAME || is_link($path)) {
            throw new InvalidArgumentException('Invalid deduplication configuration');
        }
        $directory = realpath(dirname($path));
        $this->path = (is_string($directory) ? $directory : dirname($path)) . DIRECTORY_SEPARATOR . basename($path);
    }

    public function reserve(string $messageIdHash, ?DateTimeImmutable $now = null): ?string
    {
        $this->assertHash($messageIdHash);
        $token = bin2hex(random_bytes(32));
        return $this->mutate(function (array &$claims) use ($messageIdHash, $now, $token): ?string {
            $timestamp = ($now ?? new DateTimeImmutable('now'))->getTimestamp();
            $this->prune($claims, $timestamp);
            if (isset($claims[$messageIdHash])) {
                return null;
            }
            $claims[$messageIdHash] = [
                'status' => 'reserved',
                'timestamp' => $timestamp,
                'token_hash' => hash('sha256', $token),
            ];
            return $token;
        });
    }

    public function commit(string $messageIdHash, string $token, ?DateTimeImmutable $now = null): void
    {
        $this->assertHash($messageIdHash);
        $this->mutate(function (array &$claims) use ($messageIdHash, $token, $now): null {
            $this->assertLease($claims, $messageIdHash, $token);
            $claims[$messageIdHash] = [
                'status' => 'committed',
                'timestamp' => ($now ?? new DateTimeImmutable('now'))->getTimestamp(),
            ];
            return null;
        });
    }

    public function release(string $messageIdHash, string $token): void
    {
        $this->assertHash($messageIdHash);
        $this->mutate(function (array &$claims) use ($messageIdHash, $token): null {
            $this->assertLease($claims, $messageIdHash, $token);
            unset($claims[$messageIdHash]);
            return null;
        });
    }

    /** Backwards-compatible immediate successful claim. */
    public function claim(string $messageIdHash, ?DateTimeImmutable $now = null): bool
    {
        $token = $this->reserve($messageIdHash, $now);
        if ($token === null) {
            return false;
        }
        $this->commit($messageIdHash, $token, $now);
        return true;
    }

    /** @template T @param callable(array<string,array<string,mixed>>&):T $operation @return T */
    private function mutate(callable $operation): mixed
    {
        [$directory, $directoryHandle, $directoryStat] = $this->openTrustedDirectory();
        $lockPath = $directory . DIRECTORY_SEPARATOR . self::LOCK_BASENAME;
        $lock = @fopen($lockPath, 'c+b');
        if ($lock === false) {
            fclose($directoryHandle);
            throw new RuntimeException('Deduplication store unavailable');
        }
        try {
            if (!@chmod($lockPath, 0600) || !flock($lock, LOCK_EX)) {
                throw new RuntimeException('Deduplication store unavailable');
            }
            $this->assertOpenedFile($lockPath, $lock, $directoryStat['uid']);
            $this->assertDirectoryUnchanged($directory, $directoryHandle, $directoryStat);
            $claims = $this->readClaims($directoryStat['uid']);
            $result = $operation($claims);
            ksort($claims, SORT_STRING);
            $this->writeClaims($claims, $directory, $directoryHandle, $directoryStat);
            return $result;
        } finally {
            @flock($lock, LOCK_UN);
            fclose($lock);
            fclose($directoryHandle);
        }
    }

    /** @return array{string,resource,array<string,int>} */
    private function openTrustedDirectory(): array
    {
        $directory = dirname($this->path);
        $resolved = realpath($directory);
        if (!is_string($resolved) || $resolved !== $directory || is_link($directory)) {
            throw new RuntimeException('Deduplication store unavailable');
        }
        $handle = @fopen($directory, 'rb');
        if ($handle === false) {
            throw new RuntimeException('Deduplication store unavailable');
        }
        $stat = fstat($handle);
        $pathStat = @lstat($directory);
        $euid = function_exists('posix_geteuid') ? posix_geteuid() : getmyuid();
        if (!is_array($stat) || !is_array($pathStat) || !$this->sameIdentity($stat, $pathStat)
            || (($stat['mode'] ?? 0) & 0170000) !== 0040000
            || (($stat['mode'] ?? 0) & 0777) !== 0700
            || ($stat['uid'] ?? -1) !== $euid
            || ($stat['nlink'] ?? 0) < 1) {
            fclose($handle);
            throw new RuntimeException('Deduplication store unavailable');
        }
        return [$directory, $handle, $stat];
    }

    /** @return array<string,array<string,mixed>> */
    private function readClaims(int $owner): array
    {
        if (!file_exists($this->path)) {
            if (is_link($this->path)) {
                throw new RuntimeException('Invalid deduplication state');
            }
            return [];
        }
        $handle = @fopen($this->path, 'rb');
        if ($handle === false) {
            throw new RuntimeException('Deduplication store unavailable');
        }
        try {
            $this->assertOpenedFile($this->path, $handle, $owner);
            $stat = fstat($handle);
            if (!is_array($stat) || ($stat['size'] ?? self::MAX_STATE_BYTES + 1) > self::MAX_STATE_BYTES) {
                throw new RuntimeException('Invalid deduplication state');
            }
            $contents = stream_get_contents($handle, self::MAX_STATE_BYTES + 1);
        } finally {
            fclose($handle);
        }
        if (!is_string($contents) || strlen($contents) > self::MAX_STATE_BYTES) {
            throw new RuntimeException('Deduplication store unavailable');
        }
        try {
            $decoded = json_decode($contents, false, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException $error) {
            throw new RuntimeException('Invalid deduplication state', 0, $error);
        }
        if (!$decoded instanceof \stdClass) {
            throw new RuntimeException('Invalid deduplication state');
        }
        $claims = [];
        foreach (get_object_vars($decoded) as $hash => $value) {
            $this->assertHash($hash);
            if (is_int($value)) { // migrate the original committed format
                $claims[$hash] = ['status' => 'committed', 'timestamp' => $value];
                continue;
            }
            $entry = is_object($value) ? get_object_vars($value) : null;
            if (!is_array($entry) || !is_int($entry['timestamp'] ?? null)
                || !in_array($entry['status'] ?? null, ['reserved', 'committed'], true)
                || (($entry['status'] ?? null) === 'reserved' && preg_match('/\A[a-f0-9]{64}\z/', $entry['token_hash'] ?? '') !== 1)
                || (($entry['status'] ?? null) === 'committed' && array_keys($entry) !== ['status', 'timestamp'])) {
                throw new RuntimeException('Invalid deduplication state');
            }
            $claims[$hash] = $entry;
        }
        return $claims;
    }

    /** @param array<string,array<string,mixed>> $claims */
    private function writeClaims(array $claims, string $directory, $directoryHandle, array $directoryStat): void
    {
        try {
            $json = json_encode((object) $claims, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        } catch (JsonException $error) {
            throw new RuntimeException('Deduplication store unavailable', 0, $error);
        }
        $temporary = $directory . DIRECTORY_SEPARATOR . '.' . basename($this->path) . '.tmp.' . bin2hex(random_bytes(16));
        $handle = @fopen($temporary, 'x+b');
        if ($handle === false) {
            throw new RuntimeException('Deduplication store unavailable');
        }
        try {
            if (!@chmod($temporary, 0600)) {
                throw new RuntimeException('Deduplication store unavailable');
            }
            $this->assertOpenedFile($temporary, $handle, $directoryStat['uid']);
            if (fwrite($handle, $json) !== strlen($json) || !fflush($handle)
                || !function_exists('fsync') || !fsync($handle)) {
                throw new RuntimeException('Deduplication store unavailable');
            }
            $this->assertDirectoryUnchanged($directory, $directoryHandle, $directoryStat);
            $this->assertOpenedFile($temporary, $handle, $directoryStat['uid']);
            if (!@rename($temporary, $this->path)) {
                throw new RuntimeException('Deduplication store unavailable');
            }
            $this->assertOpenedFile($this->path, $handle, $directoryStat['uid']);
            if (!fsync($directoryHandle)) {
                throw new RuntimeException('Deduplication store unavailable');
            }
        } finally {
            fclose($handle);
            if (file_exists($temporary) || is_link($temporary)) {
                @unlink($temporary);
            }
        }
    }

    private function assertOpenedFile(string $path, $handle, int $owner): void
    {
        $opened = fstat($handle);
        $named = @lstat($path);
        if (!is_array($opened) || !is_array($named) || !$this->sameIdentity($opened, $named)
            || (($opened['mode'] ?? 0) & 0170000) !== 0100000
            || (($opened['mode'] ?? 0) & 0777) !== 0600
            || ($opened['uid'] ?? -1) !== $owner || ($opened['nlink'] ?? 0) !== 1) {
            throw new RuntimeException('Invalid deduplication state');
        }
    }

    private function assertDirectoryUnchanged(string $path, $handle, array $original): void
    {
        $opened = fstat($handle);
        $named = @lstat($path);
        if (!is_array($opened) || !is_array($named) || !$this->sameIdentity($opened, $original)
            || !$this->sameIdentity($opened, $named)) {
            throw new RuntimeException('Deduplication store unavailable');
        }
    }

    private function sameIdentity(array $left, array $right): bool
    {
        return ($left['dev'] ?? null) === ($right['dev'] ?? null)
            && ($left['ino'] ?? null) === ($right['ino'] ?? null);
    }

    /** @param array<string,array<string,mixed>> $claims */
    private function prune(array &$claims, int $timestamp): void
    {
        foreach ($claims as $hash => $entry) {
            if (($entry['timestamp'] ?? 0) + $this->ttlSeconds <= $timestamp) {
                unset($claims[$hash]);
            }
        }
    }

    /** @param array<string,array<string,mixed>> $claims */
    private function assertLease(array $claims, string $hash, string $token): void
    {
        $entry = $claims[$hash] ?? null;
        if (!is_array($entry) || ($entry['status'] ?? null) !== 'reserved'
            || !is_string($entry['token_hash'] ?? null)
            || !hash_equals($entry['token_hash'], hash('sha256', $token))) {
            throw new RuntimeException('Invalid deduplication reservation');
        }
    }

    private function assertHash(string $hash): void
    {
        if (preg_match('/\A[a-f0-9]{64}\z/', $hash) !== 1) {
            throw new InvalidArgumentException('Invalid message identifier hash');
        }
    }
}
