<?php

declare(strict_types=1);

namespace XserverMail;

interface PrivateStateFilesystem
{
    public function withExclusiveLock(string $lockPath, callable $operation): mixed;

    public function assertExclusiveLockCurrent(): void;

    public function readRegular(string $path, int $limit): ?string;

    public function replaceAtomic(string $path, string $bytes, int $mode): void;
}
