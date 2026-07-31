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

    private readonly Closure $utcClock;

    public function __construct(private readonly string $path, ?callable $utcClock = null)
    {
        $this->utcClock = Closure::fromCallable(
            $utcClock ?? static fn (): DateTimeImmutable => new DateTimeImmutable('now', new DateTimeZone('UTC')),
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
        $now = ($this->utcClock)();
        if (!$now instanceof DateTimeImmutable) {
            throw new RuntimeException('Operational log unavailable');
        }
        $safeClassification = in_array($classification, self::CLASSIFICATIONS, true)
            ? $classification : 'unknown';
        $event = [
            'timestamp' => $now->setTimezone(new DateTimeZone('UTC'))->format(DATE_ATOM),
            'outcome' => $outcome,
            'message_id_hash' => preg_match('/\A[a-f0-9]{64}\z/', $messageIdHash) === 1 ? $messageIdHash : hash('sha256', $messageIdHash),
            'classification' => $safeClassification,
            'http_status' => $httpStatus,
        ];
        if ($diagnostic !== null) {
            $lastRelevant = $this->lastRelevantAttempt($diagnostic, $outcome);
            $event += [
                'attempt_count' => count($diagnostic->attempts),
                'attempt_http_statuses' => $diagnostic->attemptHttpStatuses(),
                'provider_code' => is_string($lastRelevant->providerCode)
                    ? $this->safeText($lastRelevant->providerCode, 64) : $lastRelevant->providerCode,
                'provider_description' => $this->safeText($lastRelevant->providerDescription, 200),
                'response_format' => $lastRelevant->responseFormat,
                'response_content_type' => $this->safeText($lastRelevant->responseContentType, 100),
                'response_body_bytes' => $lastRelevant->responseBodyBytes,
                'response_body_sha256' => preg_match('/\A[a-f0-9]{64}\z/D', $lastRelevant->responseBodySha256 ?? '') === 1
                    ? $lastRelevant->responseBodySha256 : null,
                'payload_bytes' => $diagnostic->payloadBytes,
                'title_characters' => $diagnostic->titleCharacters,
                'text_characters' => $diagnostic->textCharacters,
                'recovered_by_retry' => $diagnostic->recoveredByRetry,
            ];
        }
        $line = json_encode($event, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n";
        $this->append($line);
    }

    private function lastRelevantAttempt(WebhookDiagnostic $diagnostic, string $outcome): WebhookAttemptDiagnostic
    {
        $last = $diagnostic->attempts[array_key_last($diagnostic->attempts)] ?? null;
        if (!$last instanceof WebhookAttemptDiagnostic) {
            throw new RuntimeException('Operational log unavailable');
        }
        if ($outcome !== 'success' || count($diagnostic->attempts) === 1) {
            return $last;
        }
        for ($index = count($diagnostic->attempts) - 1; $index >= 0; --$index) {
            $attempt = $diagnostic->attempts[$index];
            if (!$attempt instanceof WebhookAttemptDiagnostic) {
                throw new RuntimeException('Operational log unavailable');
            }
            if (!$this->isSuccessfulAttempt($attempt)) {
                return $attempt;
            }
        }
        return $last;
    }

    private function isSuccessfulAttempt(WebhookAttemptDiagnostic $attempt): bool
    {
        return $attempt->httpStatus === 200
            && $attempt->providerCode === 200
            && $attempt->providerDescription === 'success'
            && $attempt->responseFormat === 'json';
    }

    private function safeText(?string $value, int $maximumCharacters): ?string
    {
        if ($value === null) {
            return null;
        }
        $withoutControls = preg_replace('/[\x00-\x1F\x7F]/u', '', $value);
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
        $fileHandle = null;
        try {
            NotifierConfig::assertPrivatePath($this->path);
            if (!function_exists('posix_geteuid')) {
                throw new RuntimeException('Operational log unavailable');
            }
            $owner = posix_geteuid();
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

            clearstatcache(true, $path);
            $named = @lstat($path);
            if (is_array($named)) {
                $this->assertRegularFileStat($named, $owner);
                $fileHandle = @fopen($path, 'r+b');
            } else {
                $previousUmask = umask(0077);
                try {
                    $fileHandle = @fopen($path, 'x+b');
                } finally {
                    umask($previousUmask);
                }
            }
            if (!is_resource($fileHandle)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $this->assertOpenedFile($path, $fileHandle, $owner);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
            if (!flock($fileHandle, LOCK_EX)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $this->assertOpenedFile($path, $fileHandle, $owner);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
            if (fseek($fileHandle, 0, SEEK_END) !== 0) {
                throw new RuntimeException('Operational log unavailable');
            }
            $offset = 0;
            while ($offset < strlen($line)) {
                $written = fwrite($fileHandle, substr($line, $offset));
                if (!is_int($written) || $written < 1) {
                    throw new RuntimeException('Operational log unavailable');
                }
                $offset += $written;
            }
            if (!fflush($fileHandle)) {
                throw new RuntimeException('Operational log unavailable');
            }
            $this->assertOpenedFile($path, $fileHandle, $owner);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat, $owner);
        } catch (Throwable $error) {
            throw new RuntimeException('Operational log unavailable', 0, $error);
        } finally {
            if (is_resource($fileHandle)) {
                @flock($fileHandle, LOCK_UN);
                fclose($fileHandle);
            }
            if (is_resource($directoryHandle)) {
                fclose($directoryHandle);
            }
        }
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
