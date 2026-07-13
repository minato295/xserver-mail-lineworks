<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use InvalidArgumentException;
use RuntimeException;
use Throwable;

final class SendmailClient
{
    private const MAX_MESSAGE_BYTES = 65_536;
    private const DIAGNOSTIC_BYTES_PER_STREAM = 8192;
    private const CLEANUP_STOP_TIMEOUT_SECONDS = 1.0;
    private const MAX_CLEANUP_STATUS_POLLS = 64;
    private const MAX_UNCERTAIN_DRAIN_READS = 256;
    private const MAX_DRAIN_READ_ERRORS = 16;
    private const ARGV = ['/usr/sbin/sendmail', '-t', '-i'];

    private readonly Closure $monotonicClock;
    private readonly Closure $waiter;

    public function __construct(
        private readonly SendmailProcessAdapter $adapter,
        ?callable $monotonicClock = null,
        ?callable $waiter = null,
    ) {
        $this->monotonicClock = Closure::fromCallable(
            $monotonicClock ?? static fn (): float => hrtime(true) / 1_000_000_000,
        );
        $this->waiter = Closure::fromCallable($waiter ?? static fn (): bool => usleep(1000));
    }

    public function send(string $message, int $timeoutSeconds = 15): void
    {
        if (strlen($message) > self::MAX_MESSAGE_BYTES) {
            throw new InvalidArgumentException('Invalid sendmail message');
        }
        if ($timeoutSeconds <= 0) {
            throw new InvalidArgumentException('Invalid sendmail timeout');
        }

        $handle = null;
        $closed = false;
        $timedOut = false;
        $killSent = false;
        $stdoutDiagnostic = '';
        $stderrDiagnostic = '';
        $lastClock = null;
        try {
            $handle = $this->adapter->start(self::ARGV);
            $deadline = $this->monotonicNow($lastClock) + $timeoutSeconds;
            $offset = 0;
            $stdinClosed = false;
            while (true) {
                if (!$stdinClosed) {
                    if ($offset < strlen($message)) {
                        $written = $handle->writeStdin(substr($message, $offset));
                        if ($written < 0 || $written > strlen($message) - $offset) {
                            throw new RuntimeException();
                        }
                        $offset += $written;
                    }
                    if ($offset === strlen($message)) {
                        $handle->closeStdin();
                        $stdinClosed = true;
                    }
                }

                $this->retainDiagnostic($stdoutDiagnostic, $handle->readStdout());
                $this->retainDiagnostic($stderrDiagnostic, $handle->readStderr());
                $status = $this->validatedStatus($handle->status());
                if (!$status['running']) {
                    $this->drainRemaining($handle, $stdoutDiagnostic, $stderrDiagnostic);
                    $closeCode = $handle->close();
                    $closed = true;
                    if ($timedOut) {
                        throw new RuntimeException();
                    }
                    $exitCode = $status['exitCode'] ?? $closeCode;
                    if (!$stdinClosed || $offset !== strlen($message) || $exitCode !== 0) {
                        throw new RuntimeException();
                    }
                    return;
                }

                if (!$timedOut && $this->monotonicNow($lastClock) >= $deadline) {
                    $timedOut = true;
                    if (!$stdinClosed) {
                        $handle->closeStdin();
                        $stdinClosed = true;
                    }
                    $handle->terminate(15);
                } elseif ($timedOut) {
                    if ($killSent) {
                        throw new RuntimeException();
                    }
                    $handle->terminate(9);
                    $killSent = true;
                }
                ($this->waiter)();
            }
        } catch (Throwable) {
            if ($handle instanceof SendmailProcessHandle && !$closed) {
                $this->cleanupAfterFailure($handle, $stdoutDiagnostic, $stderrDiagnostic, $timedOut);
            }
            throw new RuntimeException($timedOut ? 'Sendmail timed out' : 'Sendmail failed');
        }
    }

    private function retainDiagnostic(string &$retained, string $bytes): void
    {
        if ($bytes === '') {
            return;
        }
        $retained = substr($retained . $bytes, -self::DIAGNOSTIC_BYTES_PER_STREAM);
    }

    private function cleanupAfterFailure(
        SendmailProcessHandle $handle,
        string &$stdoutDiagnostic,
        string &$stderrDiagnostic,
        bool $signalsAlreadySent = false,
    ): void {
        try {
            $handle->closeStdin();
        } catch (Throwable) {
        }
        if (!$signalsAlreadySent) {
            foreach ([15, 9] as $signal) {
                try {
                    $handle->terminate($signal);
                } catch (Throwable) {
                }
            }
        }
        $stopped = false;
        $lastClock = null;
        try {
            $stopDeadline = $this->monotonicNow($lastClock) + self::CLEANUP_STOP_TIMEOUT_SECONDS;
        } catch (Throwable) {
            $stopDeadline = null;
        }
        for ($poll = 0; $stopDeadline !== null && $poll < self::MAX_CLEANUP_STATUS_POLLS; $poll++) {
            try {
                $stopped = !$this->validatedStatus($handle->status())['running'];
            } catch (Throwable) {
                $stopped = false;
            }
            if ($stopped) {
                break;
            }
            try {
                if ($this->monotonicNow($lastClock) >= $stopDeadline) {
                    break;
                }
            } catch (Throwable) {
                break;
            }
            try {
                $handle->terminate(9);
            } catch (Throwable) {
            }
            try {
                ($this->waiter)();
            } catch (Throwable) {
            }
        }
        $this->drainAfterFailure($handle, $stdoutDiagnostic, $stderrDiagnostic, $stopped);
        try {
            $handle->close();
        } catch (Throwable) {
        }
    }

    private function drainAfterFailure(
        SendmailProcessHandle $handle,
        string &$stdoutDiagnostic,
        string &$stderrDiagnostic,
        bool $confirmedStopped,
    ): void {
        $attempt = 0;
        $errors = 0;
        while ($confirmedStopped || $attempt < self::MAX_UNCERTAIN_DRAIN_READS) {
            ++$attempt;
            $stdoutOk = $stderrOk = true;
            try {
                $stdout = $handle->readStdout();
            } catch (Throwable) {
                $stdout = '';
                $stdoutOk = false;
                ++$errors;
            }
            try {
                $stderr = $handle->readStderr();
            } catch (Throwable) {
                $stderr = '';
                $stderrOk = false;
                ++$errors;
            }
            $this->retainDiagnostic($stdoutDiagnostic, $stdout);
            $this->retainDiagnostic($stderrDiagnostic, $stderr);
            if ($stdoutOk && $stderrOk && $stdout === '' && $stderr === '') {
                break;
            }
            if ($errors >= self::MAX_DRAIN_READ_ERRORS) {
                break;
            }
        }
    }

    private function monotonicNow(?float &$last): float
    {
        $now = ($this->monotonicClock)();
        if (!is_finite($now) || ($last !== null && $now < $last)) {
            throw new RuntimeException();
        }
        $last = $now;
        return $now;
    }

    private function drainRemaining(
        SendmailProcessHandle $handle,
        string &$stdoutDiagnostic,
        string &$stderrDiagnostic,
    ): void {
        do {
            $stdout = $handle->readStdout();
            $stderr = $handle->readStderr();
            $this->retainDiagnostic($stdoutDiagnostic, $stdout);
            $this->retainDiagnostic($stderrDiagnostic, $stderr);
        } while ($stdout !== '' || $stderr !== '');
    }

    /** @param array<mixed> $status @return array{running:bool,exitCode:?int} */
    private function validatedStatus(array $status): array
    {
        if (count($status) !== 2
            || !array_key_exists('running', $status) || !array_key_exists('exitCode', $status)
            || !is_bool($status['running'])
            || ($status['exitCode'] !== null
                && (!is_int($status['exitCode']) || $status['exitCode'] < 0))
            || ($status['running'] && $status['exitCode'] !== null)) {
            throw new RuntimeException();
        }
        return ['running' => $status['running'], 'exitCode' => $status['exitCode']];
    }
}
