<?php

declare(strict_types=1);

namespace XserverMail;

use Closure;
use RuntimeException;
use Throwable;

final class NativePrivateStateFilesystem implements PrivateStateFilesystem
{
    private readonly Closure $temporarySuffix;
    private readonly Closure $checkpoint;
    private readonly string $trustedHome;
    private readonly int $trustedUid;
    private bool $lockOperationActive = false;
    private ?string $activeDirectoryPath = null;
    /** @var resource|null */
    private $activeDirectoryHandle = null;
    /** @var array<string,int>|null */
    private ?array $activeDirectoryStat = null;
    /** @var list<array{path:string,handle:resource,stat:array<string,int>,home:bool}>|null */
    private ?array $activeChain = null;
    private ?string $activeLockPath = null;
    /** @var resource|null */
    private $activeLockHandle = null;
    /** @var array<string,int>|null */
    private ?array $activeLockStat = null;

    public function __construct(
        ?callable $temporarySuffix = null,
        ?callable $checkpoint = null,
        ?callable $accountResolver = null,
    )
    {
        $this->temporarySuffix = Closure::fromCallable(
            $temporarySuffix ?? static fn (): string => bin2hex(random_bytes(16)),
        );
        $this->checkpoint = Closure::fromCallable(
            $checkpoint ?? static function (string $name): void {},
        );
        $accountResolver ??= static function (): array {
            if (!function_exists('posix_geteuid') || !function_exists('posix_getpwuid')) {
                throw new RuntimeException('Private state unavailable');
            }
            $uid = posix_geteuid();
            $account = posix_getpwuid($uid);
            if (!is_array($account) || !isset($account['dir']) || !is_string($account['dir'])) {
                throw new RuntimeException('Private state unavailable');
            }
            return ['home' => $account['dir'], 'uid' => $uid];
        };
        $account = $accountResolver();
        if (!is_array($account) || array_keys($account) !== ['home', 'uid']
            || !is_string($account['home']) || !is_int($account['uid']) || $account['uid'] < 0
            || !$this->isCanonicalAbsolutePath($account['home'])) {
            throw new RuntimeException('Private state unavailable');
        }
        $resolvedHome = realpath($account['home']);
        if (!is_string($resolvedHome) || $resolvedHome !== $account['home']) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->trustedHome = $resolvedHome;
        $this->trustedUid = $account['uid'];
    }

    public function withExclusiveLock(string $lockPath, callable $operation): mixed
    {
        if ($this->lockOperationActive) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->lockOperationActive = true;
        try {
            ($this->checkpoint)('before_lock_open');
            return $this->withExclusiveLockOnce($lockPath, $operation);
        } finally {
            $this->lockOperationActive = false;
        }
    }

    private function withExclusiveLockOnce(string $lockPath, callable $operation): mixed
    {
        $this->assertCanonicalFilePath($lockPath);
        [$directory, $directoryHandle, $directoryStat, $directoryChain] = $this->openDirectory(dirname($lockPath));
        if (!flock($directoryHandle, LOCK_EX | LOCK_NB)) {
            $this->closeChain($directoryChain);
            throw new RuntimeException('Private state unavailable');
        }
        $existing = @lstat($lockPath);
        if (is_array($existing)) {
            if (($existing['mode'] & 0170000) !== 0100000 || ($existing['mode'] & 0777) !== 0600
                || $existing['uid'] !== $this->trustedUid || $existing['nlink'] !== 1) {
                $this->closeChain($directoryChain);
                throw new RuntimeException('Private state unavailable');
            }
            $lock = @fopen($lockPath, 'r+b');
        } else {
            $lock = @fopen($lockPath, 'x+b');
            if (is_resource($lock) && !@chmod($lockPath, 0600)) {
                fclose($lock);
                $lock = false;
            }
        }
        if (!is_resource($lock)) {
            $this->closeChain($directoryChain);
            throw new RuntimeException('Private state unavailable');
        }
        try {
            $this->assertFile($lockPath, $lock);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat);
            if (!flock($lock, LOCK_EX)) {
                throw new RuntimeException('Private state unavailable');
            }
            $this->assertFile($lockPath, $lock);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat);
            $lockStat = fstat($lock);
            if (!is_array($lockStat)) throw new RuntimeException('Private state unavailable');
            $this->activeDirectoryPath = $directory;
            $this->activeDirectoryHandle = $directoryHandle;
            $this->activeDirectoryStat = $directoryStat;
            $this->activeChain = $directoryChain;
            $this->activeLockPath = $lockPath;
            $this->activeLockHandle = $lock;
            $this->activeLockStat = $lockStat;
            try {
                ($this->checkpoint)('locked');
                $result = $operation();
                $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
                $this->assertFile($lockPath, $lock);
                return $result;
            } finally {
                $this->activeDirectoryPath = null;
                $this->activeDirectoryHandle = null;
                $this->activeDirectoryStat = null;
                $this->activeChain = null;
                $this->activeLockPath = null;
                $this->activeLockHandle = null;
                $this->activeLockStat = null;
            }
        } finally {
            @flock($lock, LOCK_UN);
            fclose($lock);
            @flock($directoryHandle, LOCK_UN);
            $this->closeChain($directoryChain);
        }
    }

    public function assertExclusiveLockCurrent(): void
    {
        ($this->checkpoint)('before_lock_lease_assert');
        if (!$this->lockOperationActive || $this->activeLockPath === null
            || !is_resource($this->activeLockHandle) || !is_array($this->activeLockStat)) {
            throw new RuntimeException('Private state unavailable');
        }
        $opened = fstat($this->activeLockHandle);
        if (!is_array($opened) || !$this->sameIdentity($opened, $this->activeLockStat)) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->assertFile($this->activeLockPath, $this->activeLockHandle);
        if ($this->activeDirectoryPath === null || !is_resource($this->activeDirectoryHandle)
            || !is_array($this->activeDirectoryStat)) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->assertBoundDirectory($this->activeDirectoryPath,
            $this->activeDirectoryHandle, $this->activeDirectoryStat);
        ($this->checkpoint)('after_lock_lease_assert');
        $this->assertFile($this->activeLockPath, $this->activeLockHandle);
    }

    public function readRegular(string $path, int $limit): ?string
    {
        if ($limit < 1) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->assertCanonicalFilePath($path);
        ($this->checkpoint)('before_read');
        $directory = dirname($path);
        $this->assertActiveDirectoryPath($directory);
        [$directory, $directoryHandle, $directoryStat, $directoryChain] = $this->openDirectory($directory);
        try {
            $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
            clearstatcache(true, $path);
            $before = @lstat($path);
            if (!is_array($before)) {
                $this->assertDirectory($directory, $directoryHandle, $directoryStat);
                clearstatcache(true, $path);
                if (is_array(@lstat($path))) {
                    throw new RuntimeException('Private state unavailable');
                }
                $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
                return null;
            }
            if (($before['mode'] & 0170000) !== 0100000 || ($before['mode'] & 0777) !== 0600
                || $before['uid'] !== $this->trustedUid || $before['nlink'] !== 1
                || $before['size'] < 1 || $before['size'] > $limit) {
                throw new RuntimeException('Private state unavailable');
            }
            $handle = @fopen($path, 'rb');
            if (!is_resource($handle)) {
                throw new RuntimeException('Private state unavailable');
            }
            try {
                $this->assertFile($path, $handle);
                $opened = fstat($handle);
                $bytes = stream_get_contents($handle, $limit + 1);
                $afterRead = fstat($handle);
                if (!is_array($opened) || !is_array($afterRead) || !$this->sameIdentity($opened, $afterRead)
                    || $opened['size'] !== $afterRead['size'] || !is_string($bytes)
                    || strlen($bytes) !== $opened['size'] || strlen($bytes) > $limit) {
                    throw new RuntimeException('Private state unavailable');
                }
                $this->assertFile($path, $handle);
            } finally {
                fclose($handle);
            }
            $this->assertDirectory($directory, $directoryHandle, $directoryStat);
            $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
            return $bytes;
        } finally {
            $this->closeChain($directoryChain);
        }
    }

    public function replaceAtomic(string $path, string $bytes, int $mode): void
    {
        if ($bytes === '' || $mode !== 0600) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->assertCanonicalFilePath($path);
        ($this->checkpoint)('before_replace');
        $directory = dirname($path);
        $this->assertActiveDirectoryPath($directory);
        [$directory, $directoryHandle, $directoryStat, $directoryChain] = $this->openDirectory($directory);
        $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
        $suffix = ($this->temporarySuffix)();
        if (!is_string($suffix) || preg_match('/\A[a-f0-9]{32}\z/D', $suffix) !== 1) {
            $this->closeChain($directoryChain);
            throw new RuntimeException('Private state unavailable');
        }
        $temporary = $directory . '/.' . basename($path) . '.tmp.' . $suffix;
        $handle = @fopen($temporary, 'x+b');
        if (!is_resource($handle)) {
            $this->closeChain($directoryChain);
            throw new RuntimeException('Private state unavailable');
        }
        try {
            if (!@chmod($temporary, 0600)) {
                throw new RuntimeException('Private state unavailable');
            }
            $this->assertFile($temporary, $handle);
            $offset = 0;
            while ($offset < strlen($bytes)) {
                $written = fwrite($handle, substr($bytes, $offset));
                if (!is_int($written) || $written <= 0 || $written > strlen($bytes) - $offset) {
                    throw new RuntimeException('Private state unavailable');
                }
                $offset += $written;
            }
            if (!fflush($handle) || !function_exists('fsync') || !fsync($handle)
                || fseek($handle, 0) !== 0) {
                throw new RuntimeException('Private state unavailable');
            }
            $readback = stream_get_contents($handle, strlen($bytes) + 1);
            if (!is_string($readback) || !hash_equals($bytes, $readback)) {
                throw new RuntimeException('Private state unavailable');
            }
            $this->assertFile($temporary, $handle);
            $this->assertDirectory($directory, $directoryHandle, $directoryStat);
            $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
            $this->assertExclusiveLockCurrent();
            if (!@rename($temporary, $path)) {
                throw new RuntimeException('Private state unavailable');
            }
            $this->assertFile($path, $handle);
            $this->assertExclusiveLockCurrent();
            if (!fsync($directoryHandle)) {
                throw new RuntimeException('Private state unavailable');
            }
            $this->assertDirectory($directory, $directoryHandle, $directoryStat);
            $this->assertBoundDirectory($directory, $directoryHandle, $directoryStat);
            $persisted = $this->readRegular($path, strlen($bytes));
            if (!is_string($persisted) || !hash_equals($bytes, $persisted)) {
                throw new RuntimeException('Private state unavailable');
            }
        } finally {
            fclose($handle);
            if (file_exists($temporary) || is_link($temporary)) {
                @unlink($temporary);
            }
            $this->closeChain($directoryChain);
        }
    }

    /** @return array{string,resource,array<string,int>,list<array{path:string,handle:resource,stat:array<string,int>,home:bool}>} */
    private function openDirectory(string $directory): array
    {
        if (!$this->isCanonicalAbsolutePath($directory)
            || !str_starts_with($directory, $this->trustedHome . '/')) {
            throw new RuntimeException('Private state unavailable');
        }
        $relative = substr($directory, strlen($this->trustedHome) + 1);
        $components = explode('/', $relative);
        if ($relative === '' || in_array('', $components, true)
            || in_array('.', $components, true) || in_array('..', $components, true)) {
            throw new RuntimeException('Private state unavailable');
        }
        $paths = [$this->trustedHome];
        $path = $this->trustedHome;
        foreach ($components as $component) {
            $path .= '/' . $component;
            $paths[] = $path;
        }
        $chain = [];
        try {
            foreach ($paths as $index => $path) {
                if (realpath($path) !== $path || is_link($path)) {
                    throw new RuntimeException('Private state unavailable');
                }
                $handle = @fopen($path, 'rb');
                if (!is_resource($handle)) {
                    throw new RuntimeException('Private state unavailable');
                }
                $stat = fstat($handle);
                $named = @lstat($path);
                $home = $index === 0;
                if (!is_array($stat) || !is_array($named) || !$this->sameIdentity($stat, $named)
                    || !$this->isTrustedDirectoryStat($stat, $home)) {
                    fclose($handle);
                    throw new RuntimeException('Private state unavailable');
                }
                $chain[] = ['path' => $path, 'handle' => $handle, 'stat' => $stat, 'home' => $home];
            }
            $this->assertChain($chain);
        } catch (Throwable $exception) {
            $this->closeChain($chain);
            throw $exception;
        }
        $last = $chain[array_key_last($chain)];
        return [$directory, $last['handle'], $last['stat'], $chain];
    }

    /** @param resource $handle */
    private function assertFile(string $path, $handle): void
    {
        $opened = fstat($handle);
        $named = @lstat($path);
        if (!is_array($opened) || !is_array($named) || !$this->sameIdentity($opened, $named)
            || ($opened['mode'] & 0170000) !== 0100000 || ($opened['mode'] & 0777) !== 0600
            || $opened['uid'] !== $this->trustedUid || $opened['nlink'] !== 1) {
            throw new RuntimeException('Private state unavailable');
        }
    }

    /** @param resource $handle @param array<string,int> $original */
    private function assertDirectory(string $path, $handle, array $original): void
    {
        $opened = fstat($handle);
        $named = @lstat($path);
        if (!is_array($opened) || !is_array($named) || !$this->sameIdentity($opened, $original)
            || !$this->sameIdentity($opened, $named) || !$this->isTrustedDirectoryStat($opened, false)) {
            throw new RuntimeException('Private state unavailable');
        }
    }

    private function assertActiveDirectoryPath(string $directory): void
    {
        if ($this->activeDirectoryPath === null) {
            return;
        }
        if ($directory !== $this->activeDirectoryPath
            || !is_resource($this->activeDirectoryHandle)
            || !is_array($this->activeDirectoryStat) || !is_array($this->activeChain)) {
            throw new RuntimeException('Private state unavailable');
        }
        $this->assertChain($this->activeChain);
        $this->assertDirectory(
            $this->activeDirectoryPath,
            $this->activeDirectoryHandle,
            $this->activeDirectoryStat,
        );
    }

    /** @param resource $handle @param array<string,int> $stat */
    private function assertBoundDirectory(string $directory, $handle, array $stat): void
    {
        $this->assertActiveDirectoryPath($directory);
        if ($this->activeDirectoryStat === null) {
            return;
        }
        $opened = fstat($handle);
        if (!is_array($opened) || !$this->sameIdentity($opened, $stat)
            || !$this->sameIdentity($opened, $this->activeDirectoryStat)) {
            throw new RuntimeException('Private state unavailable');
        }
    }

    /** @param array<string,int> $left @param array<string,int> $right */
    private function sameIdentity(array $left, array $right): bool
    {
        return ($left['dev'] ?? null) === ($right['dev'] ?? null)
            && ($left['ino'] ?? null) === ($right['ino'] ?? null);
    }

    /** @param list<array{path:string,handle:resource,stat:array<string,int>,home:bool}> $chain */
    private function assertChain(array $chain): void
    {
        foreach ($chain as $entry) {
            $opened = fstat($entry['handle']);
            $named = @lstat($entry['path']);
            if (!is_array($opened) || !is_array($named)
                || !$this->sameIdentity($opened, $entry['stat'])
                || !$this->sameIdentity($opened, $named)
                || !$this->isTrustedDirectoryStat($opened, $entry['home'])) {
                throw new RuntimeException('Private state unavailable');
            }
        }
    }

    /** @param array<string,int> $stat */
    private function isTrustedDirectoryStat(array $stat, bool $home): bool
    {
        $mode = $stat['mode'] ?? 0;
        return ($mode & 0170000) === 0040000
            && ($stat['uid'] ?? -1) === $this->trustedUid
            && ($stat['nlink'] ?? 0) >= 1
            && ($home ? (($mode & 0022) === 0) : (($mode & 0777) === 0700));
    }

    /** @param list<array{path:string,handle:resource,stat:array<string,int>,home:bool}> $chain */
    private function closeChain(array $chain): void
    {
        foreach (array_reverse($chain) as $entry) {
            if (is_resource($entry['handle'])) {
                fclose($entry['handle']);
            }
        }
    }

    private function isCanonicalAbsolutePath(string $path): bool
    {
        return $path !== '' && $path[0] === '/' && $path !== '/'
            && !str_ends_with($path, '/') && !str_contains($path, '//')
            && preg_match('~(?:\A|/)(?:\.|\.\.)(?:/|\z)~D', $path) !== 1;
    }

    private function assertCanonicalFilePath(string $path): void
    {
        if (!$this->isCanonicalAbsolutePath($path)
            || basename($path) === '' || basename($path) === '.' || basename($path) === '..') {
            throw new RuntimeException('Private state unavailable');
        }
    }
}
