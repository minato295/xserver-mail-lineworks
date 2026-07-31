<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use DateTimeImmutable;
use DateTimeZone;
use RuntimeException;
use Throwable;

final class OperationalLogger
{
    private const CLASSIFICATIONS = [
        'success', 'invalid_payload', 'invalid_parameter', 'missing_parameter',
        'invalid_webhook_url', 'rate_limited', 'http_error', 'transport_error',
        'forced_test_failure', 'internal_error', 'system_mail_suppressed',
        'health_state_failure', 'unknown', 'dedup_store_failure',
        'non_target_recipient',
    ];
    private const RESPONSE_FORMATS = ['json', 'invalid_json', 'transport_error'];
    private const MAX_DIAGNOSTIC_INTEGER = 2_147_483_647;
    private const MAX_LOG_BYTES = 240 * 1024;
    private const MAX_EVENT_BYTES = 120 * 1024;

    private readonly Closure $utcClock;
    private readonly Closure $effectiveUid;
    private readonly ?Closure $beforeAtomicReplace;
    private readonly ?Closure $faultInjector;
    private readonly Closure $writer;

    public function __construct(
        private readonly string $path,
        ?callable $utcClock = null,
        ?callable $effectiveUid = null,
        ?callable $beforeAtomicReplace = null,
        ?callable $faultInjector = null,
        ?callable $writer = null,
    ) {
        $this->utcClock = Closure::fromCallable(
            $utcClock ?? static fn (): DateTimeImmutable => new DateTimeImmutable('now', new DateTimeZone('UTC')),
        );
        $this->effectiveUid = Closure::fromCallable($effectiveUid ?? static function (): int {
            if (!function_exists('posix_geteuid')) {
                throw new RuntimeException('Operational log unavailable');
            }
            return posix_geteuid();
        });
        $this->beforeAtomicReplace = $beforeAtomicReplace === null
            ? null : Closure::fromCallable($beforeAtomicReplace);
        $this->faultInjector = $faultInjector === null
            ? null : Closure::fromCallable($faultInjector);
        $this->writer = Closure::fromCallable(
            $writer ?? static fn ($handle, string $bytes): int|false => fwrite($handle, $bytes),
        );
    }

    public function log(
        string $outcome,
        string $messageIdHash,
        string $classification,
        ?int $httpStatus,
        ?WebhookDiagnostic $diagnostic = null,
    ): void
    {
        $this->assertCommonContract($outcome, $classification, $httpStatus, $diagnostic !== null);
        $now = ($this->utcClock)();
        if (!$now instanceof DateTimeImmutable) {
            throw new RuntimeException('Operational log unavailable');
        }
        $safeClassification = $classification;
        $event = [
            'timestamp' => $now->setTimezone(new DateTimeZone('UTC'))->format(DATE_ATOM),
            'outcome' => $outcome,
            'message_id_hash' => preg_match('/\A[a-f0-9]{64}\z/', $messageIdHash) === 1 ? $messageIdHash : hash('sha256', $messageIdHash),
            'classification' => $safeClassification,
            'http_status' => $httpStatus,
        ];
        if ($diagnostic !== null) {
            [$attempts, $lastRelevant, $safeClassification] = $this->validatedDiagnostic(
                $diagnostic, $outcome, $classification, $httpStatus,
            );
            $event['classification'] = $safeClassification;
            $event += [
                'attempt_count' => count($attempts),
                'attempt_http_statuses' => array_column($attempts, 'http_status'),
                'provider_code' => $lastRelevant['provider_code'],
                'provider_description' => $lastRelevant['provider_description'],
                'response_format' => $lastRelevant['response_format'],
                'response_content_type' => $lastRelevant['response_content_type'],
                'response_body_bytes' => $lastRelevant['response_body_bytes'],
                'response_body_sha256' => $lastRelevant['response_body_sha256'],
                'payload_bytes' => $diagnostic->payloadBytes,
                'title_characters' => $diagnostic->titleCharacters,
                'text_characters' => $diagnostic->textCharacters,
                'recovered_by_retry' => $diagnostic->recoveredByRetry,
            ];
        }
        $line = json_encode($event, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        if (strlen($line) > self::MAX_EVENT_BYTES) {
            throw new RuntimeException('Operational log unavailable');
        }
        $this->append($line);
    }

    private function assertCommonContract(
        string $outcome,
        string $classification,
        ?int $httpStatus,
        bool $hasDiagnostic,
    ): void
    {
        if (!in_array($outcome, ['success', 'failure', 'ignored'], true)
            || !in_array($classification, self::CLASSIFICATIONS, true)
            || ($httpStatus !== null && ($httpStatus < 100 || $httpStatus > 599))) {
            throw new RuntimeException('Operational log unavailable');
        }
        if ($outcome === 'ignored') {
            if ($classification !== 'non_target_recipient' || $httpStatus !== null || $hasDiagnostic) {
                throw new RuntimeException('Operational log unavailable');
            }
            return;
        }
        if ($outcome === 'success') {
            if ($hasDiagnostic) {
                if (!in_array($classification, ['success', 'internal_error'], true) || $httpStatus !== 200) {
                    throw new RuntimeException('Operational log unavailable');
                }
            } elseif (!(($classification === 'success' && $httpStatus === 200)
                || ($classification === 'system_mail_suppressed' && $httpStatus === null))) {
                throw new RuntimeException('Operational log unavailable');
            }
            return;
        }
        if (in_array($classification, ['success', 'non_target_recipient'], true)) {
            throw new RuntimeException('Operational log unavailable');
        }
    }

    /**
     * @return array{0:list<array<string,mixed>>,1:array<string,mixed>,2:string}
     */
    private function validatedDiagnostic(
        WebhookDiagnostic $diagnostic,
        string $outcome,
        string $classification,
        ?int $httpStatus,
    ): array
    {
        if (!array_is_list($diagnostic->attempts) || $diagnostic->attempts === []
            || !$this->isBoundedInteger($diagnostic->payloadBytes)
            || !$this->isBoundedInteger($diagnostic->titleCharacters)
            || !$this->isBoundedInteger($diagnostic->textCharacters)) {
            throw new RuntimeException('Operational log unavailable');
        }
        $attempts = [];
        foreach ($diagnostic->attempts as $attempt) {
            if (!$attempt instanceof WebhookAttemptDiagnostic) {
                throw new RuntimeException('Operational log unavailable');
            }
            $attempts[] = $this->validatedAttempt($attempt);
        }
        $lastIndex = array_key_last($attempts);
        if (!is_int($lastIndex) || $httpStatus !== $attempts[$lastIndex]['http_status']) {
            throw new RuntimeException('Operational log unavailable');
        }
        foreach (array_slice($attempts, 0, -1) as $attempt) {
            if ($attempt['http_status'] === null) {
                throw new RuntimeException('Operational log unavailable');
            }
        }

        $recovered = false;
        for ($index = 0; $index + 1 < count($attempts); ++$index) {
            $status = $attempts[$index]['http_status'];
            if (($status === 429 || (is_int($status) && $status >= 500 && $status <= 599))
                && $this->isSuccessfulAttempt($attempts[$index + 1])) {
                $recovered = true;
                break;
            }
        }
        if ($diagnostic->recoveredByRetry !== $recovered) {
            throw new RuntimeException('Operational log unavailable');
        }

        $relevantIndex = $lastIndex;
        if ($outcome === 'success' && count($attempts) > 1) {
            for ($index = $lastIndex; $index >= 0; --$index) {
                if (!$this->isSuccessfulAttempt($attempts[$index])) {
                    $relevantIndex = $index;
                    break;
                }
            }
        }
        $relevant = $attempts[$relevantIndex];
        if ($outcome === 'failure') {
            if ($this->isSuccessfulAttempt($relevant)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $expectedClassification = $this->attemptClassification($relevant);
            if ($relevant['response_format'] === 'json'
                && $relevant['provider_description'] === null
                && in_array($classification, [
                    'invalid_parameter', 'missing_parameter', 'invalid_webhook_url', 'rate_limited',
                ], true)) {
                $classification = 'http_error';
            }
            if ($classification !== $expectedClassification) {
                throw new RuntimeException('Operational log unavailable');
            }
        } elseif (count($attempts) === 1) {
            if (!$this->isSuccessfulAttempt($attempts[0]) || $diagnostic->recoveredByRetry) {
                throw new RuntimeException('Operational log unavailable');
            }
        } else {
            if (!$this->isSuccessfulAttempt($attempts[$lastIndex]) || $relevantIndex === $lastIndex) {
                throw new RuntimeException('Operational log unavailable');
            }
            if (!$diagnostic->recoveredByRetry) {
                if ($relevantIndex !== 0 || $this->attemptClassification($relevant) !== 'invalid_parameter') {
                    throw new RuntimeException('Operational log unavailable');
                }
                foreach (array_slice($attempts, 1) as $attempt) {
                    if (!$this->isSuccessfulAttempt($attempt)) {
                        throw new RuntimeException('Operational log unavailable');
                    }
                }
            }
        }
        return [$attempts, $relevant, $classification];
    }

    /** @return array<string,mixed> */
    private function validatedAttempt(WebhookAttemptDiagnostic $attempt): array
    {
        $status = $attempt->httpStatus;
        $format = $attempt->responseFormat;
        $code = is_string($attempt->providerCode)
            ? $this->safeText($attempt->providerCode, 64) : $attempt->providerCode;
        $description = $this->safeText($attempt->providerDescription, 200);
        $contentType = $this->safeText($attempt->responseContentType, 100);
        $bodyHash = $attempt->responseBodySha256;
        if (($status !== null && ($status < 100 || $status > 599))
            || !in_array($format, self::RESPONSE_FORMATS, true)
            || !$this->isBoundedInteger($attempt->responseBodyBytes)
            || ($bodyHash !== null && preg_match('/\A[a-f0-9]{64}\z/D', $bodyHash) !== 1)) {
            throw new RuntimeException('Operational log unavailable');
        }
        if ($format === 'transport_error') {
            if ($status !== null || $code !== null || $description !== null || $contentType !== null
                || $attempt->responseBodyBytes !== 0 || $bodyHash !== null) {
                throw new RuntimeException('Operational log unavailable');
            }
        } else {
            if ($status === null || $bodyHash === null) {
                throw new RuntimeException('Operational log unavailable');
            }
            if ($format === 'invalid_json' && ($code !== null || $description !== null)) {
                throw new RuntimeException('Operational log unavailable');
            }
        }
        return [
            'http_status' => $status,
            'provider_code' => $code,
            'provider_description' => $description,
            'response_format' => $format,
            'response_content_type' => $contentType,
            'response_body_bytes' => $attempt->responseBodyBytes,
            'response_body_sha256' => $bodyHash,
        ];
    }

    /** @param array<string,mixed> $attempt */
    private function isSuccessfulAttempt(array $attempt): bool
    {
        return $attempt['http_status'] === 200
            && $attempt['provider_code'] === 200
            && $attempt['provider_description'] === 'success'
            && $attempt['response_format'] === 'json';
    }

    /** @param array<string,mixed> $attempt */
    private function attemptClassification(array $attempt): string
    {
        return match ($attempt['response_format']) {
            'transport_error' => 'transport_error',
            'invalid_json' => 'http_error',
            default => match ($attempt['provider_description']) {
                'invalid parameter' => 'invalid_parameter',
                'missing parameter' => 'missing_parameter',
                'invalid webhook URL' => 'invalid_webhook_url',
                'too many request' => 'rate_limited',
                default => 'http_error',
            },
        };
    }

    private function isBoundedInteger(int $value): bool
    {
        return $value >= 0 && $value <= self::MAX_DIAGNOSTIC_INTEGER;
    }

    private function safeText(?string $value, int $maximumCharacters): ?string
    {
        if ($value === null) {
            return null;
        }
        $withoutControls = preg_replace('/[\x00-\x1F\x7F-\x9F]/u', '', $value);
        if (!is_string($withoutControls)) {
            return null;
        }
        $characters = preg_split('//u', trim($withoutControls), -1, PREG_SPLIT_NO_EMPTY);
        if (!is_array($characters)) {
            return null;
        }
        return implode('', array_slice($characters, 0, $maximumCharacters));
    }

    private function append(string $line): void
    {
        $directoryHandle = null;
        $lockHandle = null;
        $fileHandle = null;
        $temporaryHandle = null;
        $temporaryPath = null;
        try {
            NotifierConfig::assertPrivatePath($this->path);
            $owner = ($this->effectiveUid)();
            if (!is_int($owner) || $owner < 0) {
                throw new RuntimeException('Operational log unavailable');
            }
            $directory = dirname($this->path);
            $resolvedDirectory = realpath($directory);
            if (!is_string($resolvedDirectory) || is_link($directory)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $directory = $resolvedDirectory;
            $path = $directory . DIRECTORY_SEPARATOR . basename($this->path);
            $directoryHandle = @fopen($directory, 'rb');
            if (!is_resource($directoryHandle)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $directoryStat = fstat($directoryHandle);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);

            $lockPath = $path . '.lock';
            $lockHandle = $this->openOrCreatePrivateFile($lockPath, $owner);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
            if (!flock($lockHandle, LOCK_EX)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $this->assertOpenedFile($lockPath, $lockHandle, $owner);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);

            // The fixed sidecar lock, not the replaceable log inode, serializes every writer.
            $fileHandle = $this->openOrCreatePrivateFile($path, $owner);
            $this->assertOpenedFile($path, $fileHandle, $owner);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
            $opened = fstat($fileHandle);
            if (!is_array($opened) || !is_int($opened['size'] ?? null) || $opened['size'] < 0) {
                throw new RuntimeException('Operational log unavailable');
            }
            $expectedSize = $opened['size'] + strlen($line);
            if ($expectedSize <= self::MAX_LOG_BYTES && $this->hasCompleteTail($fileHandle, $opened['size'])) {
                if (fseek($fileHandle, 0, SEEK_END) !== 0) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $this->writeAll($fileHandle, $line);
                if (!fflush($fileHandle)) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $this->syncFile($fileHandle);
                $this->assertOpenedFile($path, $fileHandle, $owner);
                $completedHandle = $fileHandle;
            } else {
                $retained = $this->compactedTail(
                    $fileHandle,
                    $opened['size'],
                    self::MAX_LOG_BYTES - strlen($line),
                );
                $compacted = $retained . $line;
                $expectedSize = strlen($compacted);
                if ($expectedSize > self::MAX_LOG_BYTES) {
                    throw new RuntimeException('Operational log unavailable');
                }

                [$temporaryPath, $temporaryHandle] = $this->createPrivateTemporaryFile(
                    $directory, basename($path), $owner,
                );
                $this->injectFault('temp_created', $temporaryPath);
                $this->assertOpenedFile($temporaryPath, $temporaryHandle, $owner);
                $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
                $this->writeAll($temporaryHandle, $compacted);
                $this->injectFault('before_temp_flush', $temporaryPath);
                if (!fflush($temporaryHandle)) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $this->syncFile($temporaryHandle);
                $temporaryStat = fstat($temporaryHandle);
                $this->assertOpenedFile($temporaryPath, $temporaryHandle, $owner);
                if (!is_array($temporaryStat) || ($temporaryStat['size'] ?? -1) !== $expectedSize
                    || $expectedSize > self::MAX_LOG_BYTES) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $this->assertOpenedFile($path, $fileHandle, $owner);
                $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
                if ($this->beforeAtomicReplace !== null) {
                    ($this->beforeAtomicReplace)();
                }
                $this->injectFault('before_rename', $temporaryPath);
                if (!@rename($temporaryPath, $path)) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $temporaryPath = null;
                $this->syncDirectory($directoryHandle);
                $this->injectFault('after_rename', null);
                $this->assertOpenedFile($path, $temporaryHandle, $owner);
                $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
                $completedHandle = $temporaryHandle;
            }

            $completed = fstat($completedHandle);
            if (!is_array($completed) || ($completed['size'] ?? -1) !== $expectedSize
                || $expectedSize > self::MAX_LOG_BYTES) {
                throw new RuntimeException('Operational log unavailable');
            }
        } catch (Throwable $error) {
            throw new RuntimeException('Operational log unavailable', 0, $error);
        } finally {
            if (is_resource($temporaryHandle)) {
                fclose($temporaryHandle);
            }
            if (is_string($temporaryPath)) {
                $this->cleanupTemporaryPath($temporaryPath);
            }
            if (is_resource($fileHandle)) {
                fclose($fileHandle);
            }
            if (is_resource($lockHandle)) {
                @flock($lockHandle, LOCK_UN);
                fclose($lockHandle);
            }
            if (is_resource($directoryHandle)) {
                fclose($directoryHandle);
            }
        }
    }

    /** @return resource */
    private function openOrCreatePrivateFile(string $path, int $owner)
    {
        clearstatcache(true, $path);
        $named = @lstat($path);
        if (is_array($named)) {
            $this->assertRegularFileStat($named, $owner);
            $handle = @fopen($path, 'r+b');
        } else {
            $previousUmask = umask(0077);
            try {
                $handle = @fopen($path, 'x+b');
            } finally {
                umask($previousUmask);
            }
            if (!is_resource($handle)) {
                clearstatcache(true, $path);
                $named = @lstat($path);
                if (!is_array($named)) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $this->assertRegularFileStat($named, $owner);
                $handle = @fopen($path, 'r+b');
            }
        }
        if (!is_resource($handle)) {
            throw new RuntimeException('Operational log unavailable');
        }
        $this->assertOpenedFile($path, $handle, $owner);
        return $handle;
    }

    /** @return array{0:string,1:resource} */
    private function createPrivateTemporaryFile(string $directory, string $basename, int $owner): array
    {
        for ($attempt = 0; $attempt < 8; ++$attempt) {
            $path = $directory . DIRECTORY_SEPARATOR . '.' . $basename . '.tmp.' . bin2hex(random_bytes(16));
            $previousUmask = umask(0077);
            try {
                $handle = @fopen($path, 'x+b');
            } finally {
                umask($previousUmask);
            }
            if (is_resource($handle)) {
                $this->assertOpenedFile($path, $handle, $owner);
                return [$path, $handle];
            }
        }
        throw new RuntimeException('Operational log unavailable');
    }

    /** @param resource $handle */
    private function hasCompleteTail($handle, int $size): bool
    {
        if ($size === 0) {
            return true;
        }
        if (fseek($handle, -1, SEEK_END) !== 0) {
            throw new RuntimeException('Operational log unavailable');
        }
        return fread($handle, 1) === "\n";
    }

    /** @param resource $handle */
    private function syncFile($handle): void
    {
        if (function_exists('fsync') && !fsync($handle)) {
            throw new RuntimeException('Operational log unavailable');
        }
    }

    /** @param resource $handle */
    private function syncDirectory($handle): void
    {
        if (function_exists('fsync')) {
            @fsync($handle);
        }
    }

    private function injectFault(string $phase, ?string $temporaryPath): void
    {
        if ($this->faultInjector !== null) {
            ($this->faultInjector)($phase, $temporaryPath);
        }
    }

    private function cleanupTemporaryPath(string $path): void
    {
        clearstatcache(true, $path);
        $named = @lstat($path);
        if (is_array($named)
            && ((($named['mode'] ?? 0) & 0170000) === 0100000 || (($named['mode'] ?? 0) & 0170000) === 0120000)) {
            @unlink($path);
        }
    }

    /** @param resource $handle */
    private function writeAll($handle, string $bytes): void
    {
        $offset = 0;
        while ($offset < strlen($bytes)) {
            $written = ($this->writer)($handle, substr($bytes, $offset));
            if (!is_int($written) || $written < 1) {
                throw new RuntimeException('Operational log unavailable');
            }
            $offset += $written;
        }
    }

    /** @param resource $handle */
    private function compactedTail($handle, int $size, int $budget): string
    {
        if ($budget < self::MAX_EVENT_BYTES) {
            throw new RuntimeException('Operational log unavailable');
        }
        $readBytes = min($size, self::MAX_LOG_BYTES);
        $start = $size - $readBytes;
        if (fseek($handle, $start, SEEK_SET) !== 0) {
            throw new RuntimeException('Operational log unavailable');
        }
        $tail = '';
        while (strlen($tail) < $readBytes) {
            $chunk = fread($handle, $readBytes - strlen($tail));
            if (!is_string($chunk) || $chunk === '') {
                throw new RuntimeException('Operational log unavailable');
            }
            $tail .= $chunk;
        }
        if ($start > 0) {
            $firstBoundary = strpos($tail, "\n");
            if ($firstBoundary === false) {
                throw new RuntimeException('Operational log unavailable');
            }
            $tail = substr($tail, $firstBoundary + 1);
        }
        $lastBoundary = strrpos($tail, "\n");
        if ($lastBoundary === false) {
            if ($start > 0) {
                throw new RuntimeException('Operational log unavailable');
            }
            return '';
        }
        $lines = explode("\n", substr($tail, 0, $lastBoundary));
        $selected = [];
        $selectedBytes = 0;
        $foundValid = false;
        for ($index = count($lines) - 1; $index >= 0; --$index) {
            $candidate = $lines[$index];
            if ($candidate === '' || preg_match('//u', $candidate) !== 1) {
                continue;
            }
            try {
                $decoded = json_decode($candidate, false, 32, JSON_THROW_ON_ERROR);
            } catch (Throwable) {
                continue;
            }
            if (!$decoded instanceof \stdClass) {
                continue;
            }
            $record = $candidate . "\n";
            if (!$foundValid && strlen($record) > $budget) {
                throw new RuntimeException('Operational log unavailable');
            }
            $foundValid = true;
            if ($selectedBytes + strlen($record) > $budget) {
                break;
            }
            array_unshift($selected, $record);
            $selectedBytes += strlen($record);
        }
        if (!$foundValid && $start > 0) {
            throw new RuntimeException('Operational log unavailable');
        }
        return implode('', $selected);
    }

    /** @param resource $handle @param array<string,int>|false $original */
    private function assertDirectory(string $path, $handle, array|false $original, int $owner): void
    {
        $opened = fstat($handle);
        $named = @lstat($path);
        if (!is_array($original) || !is_array($opened) || !is_array($named)
            || !$this->sameIdentity($opened, $original) || !$this->sameIdentity($opened, $named)
            || (($opened['mode'] ?? 0) & 0170000) !== 0040000
            || (($opened['mode'] ?? 0) & 0777) !== 0700
            || ($opened['uid'] ?? -1) !== $owner || ($opened['nlink'] ?? 0) < 1) {
            throw new RuntimeException('Operational log unavailable');
        }
    }

    /** @param resource $handle */
    private function assertOpenedFile(string $path, $handle, int $owner): void
    {
        $opened = fstat($handle);
        $named = @lstat($path);
        if (!is_array($opened) || !is_array($named) || !$this->sameIdentity($opened, $named)) {
            throw new RuntimeException('Operational log unavailable');
        }
        $this->assertRegularFileStat($opened, $owner);
    }

    /** @param array<string,int> $stat */
    private function assertRegularFileStat(array $stat, int $owner): void
    {
        if ((($stat['mode'] ?? 0) & 0170000) !== 0100000
            || (($stat['mode'] ?? 0) & 0777) !== 0600
            || ($stat['uid'] ?? -1) !== $owner || ($stat['nlink'] ?? 0) !== 1) {
            throw new RuntimeException('Operational log unavailable');
        }
    }

    /** @param array<string,int> $left @param array<string,int> $right */
    private function sameIdentity(array $left, array $right): bool
    {
        return ($left['dev'] ?? null) === ($right['dev'] ?? null)
            && ($left['ino'] ?? null) === ($right['ino'] ?? null);
    }
}
