<?php

declare(strict_types=1);

namespace XserverMail;

interface SendmailProcessHandle
{
    public function writeStdin(string $bytes): int;

    public function closeStdin(): void;

    public function readStdout(): string;

    public function readStderr(): string;

    /** @return array{running:bool,exitCode:?int} */
    public function status(): array;

    public function terminate(int $signal): void;

    public function close(): int;
}

interface SendmailProcessAdapter
{
    /** @param list<string> $argv */
    public function start(array $argv): SendmailProcessHandle;
}
