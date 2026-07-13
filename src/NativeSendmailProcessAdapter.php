<?php

declare(strict_types=1);

namespace XserverMail;

use RuntimeException;

final class NativeSendmailProcessAdapter implements SendmailProcessAdapter
{
    public function start(array $argv): SendmailProcessHandle
    {
        $pipes = [];
        $process = @proc_open($argv, [
            0 => ['pipe', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ], $pipes);
        if (!is_resource($process)) {
            throw new RuntimeException('Unable to start sendmail process');
        }
        if (count($pipes) !== 3 || array_keys($pipes) !== [0, 1, 2]) {
            $this->cleanupFailedStart($process, $pipes);
            throw new RuntimeException('Unable to start sendmail process');
        }
        foreach ($pipes as $pipe) {
            if (!is_resource($pipe) || !stream_set_blocking($pipe, false)) {
                $this->cleanupFailedStart($process, $pipes);
                throw new RuntimeException('Unable to configure sendmail process');
            }
        }
        return new NativeSendmailProcessHandle($process, $pipes);
    }

    /** @param resource $process @param array<int,mixed> $pipes */
    private function cleanupFailedStart($process, array $pipes): void
    {
        foreach ($pipes as $pipe) {
            if (is_resource($pipe)) {
                fclose($pipe);
            }
        }
        @proc_terminate($process, 9);
        @proc_close($process);
    }
}

final class NativeSendmailProcessHandle implements SendmailProcessHandle
{
    /** @var resource|null */
    private $process;
    /** @var array<int,resource|null> */
    private array $pipes;
    private ?int $observedExitCode = null;

    /** @param resource $process @param array<int,resource> $pipes */
    public function __construct($process, array $pipes)
    {
        $this->process = $process;
        $this->pipes = $pipes;
    }

    public function writeStdin(string $bytes): int
    {
        $pipe = $this->pipes[0] ?? null;
        if (!is_resource($pipe)) {
            throw new RuntimeException('Sendmail stdin unavailable');
        }
        $written = @fwrite($pipe, $bytes);
        if ($written === false) {
            throw new RuntimeException('Sendmail stdin write failed');
        }
        return $written;
    }

    public function closeStdin(): void
    {
        $this->closePipe(0);
    }

    public function readStdout(): string
    {
        return $this->readPipe(1);
    }

    public function readStderr(): string
    {
        return $this->readPipe(2);
    }

    public function status(): array
    {
        if (!is_resource($this->process)) {
            return ['running' => false, 'exitCode' => $this->observedExitCode];
        }
        $status = proc_get_status($this->process);
        if (!is_array($status)) {
            throw new RuntimeException('Sendmail status unavailable');
        }
        if (!$status['running'] && is_int($status['exitcode']) && $status['exitcode'] >= 0) {
            $this->observedExitCode = $status['exitcode'];
        }
        return ['running' => (bool) $status['running'], 'exitCode' => $this->observedExitCode];
    }

    public function terminate(int $signal): void
    {
        if (is_resource($this->process)) {
            @proc_terminate($this->process, $signal);
        }
    }

    public function close(): int
    {
        foreach (array_keys($this->pipes) as $index) {
            $this->closePipe($index);
        }
        if (!is_resource($this->process)) {
            return $this->observedExitCode ?? -1;
        }
        $result = proc_close($this->process);
        $this->process = null;
        return $this->observedExitCode ?? $result;
    }

    private function readPipe(int $index): string
    {
        $pipe = $this->pipes[$index] ?? null;
        if (!is_resource($pipe)) {
            return '';
        }
        $bytes = stream_get_contents($pipe);
        if ($bytes === false) {
            throw new RuntimeException('Sendmail output read failed');
        }
        return $bytes;
    }

    private function closePipe(int $index): void
    {
        $pipe = $this->pipes[$index] ?? null;
        if (is_resource($pipe)) {
            fclose($pipe);
        }
        $this->pipes[$index] = null;
    }
}
