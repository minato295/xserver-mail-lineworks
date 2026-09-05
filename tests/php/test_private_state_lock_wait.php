<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';

use XserverMail\DeliveryOutbox;
use XserverMail\NativePrivateStateFilesystem;

function waitCheck(bool $ok, string $message): void
{
    if (!$ok) { throw new RuntimeException($message); }
}

function waitRefuses(callable $operation): void
{
    try { $operation(); } catch (RuntimeException) { return; }
    throw new RuntimeException('Contended or replaced lock must refuse');
}

function waitCleanup(string $path): void
{
    foreach (new FilesystemIterator($path) as $item) {
        if ($item->isDir() && !$item->isLink()) { waitCleanup($item->getPathname()); }
        else { unlink($item->getPathname()); }
    }
    rmdir($path);
}

$home = sys_get_temp_dir() . '/private-lock-wait-' . bin2hex(random_bytes(8));
mkdir($home, 0700);
$home = realpath($home);
waitCheck(is_string($home), 'Fixture home must resolve');
$account = static fn (): array => ['home' => $home, 'uid' => posix_geteuid()];

try {
    // A different process holds the same directory as a health alert would.
    // No transport is started: only the real local lock and outbox are exercised.
    mkdir($home . '/concurrent', 0700);
    $sockets = stream_socket_pair(STREAM_PF_UNIX, STREAM_SOCK_STREAM, STREAM_IPPROTO_IP);
    waitCheck(is_array($sockets), 'Synchronization sockets must open');
    $pid = pcntl_fork();
    waitCheck($pid >= 0, 'Lock holder must fork');
    if ($pid === 0) {
        fclose($sockets[0]);
        $holder = fopen($home . '/concurrent', 'rb');
        if (!is_resource($holder) || !flock($holder, LOCK_EX)) { exit(2); }
        fwrite($sockets[1], 'L');
        usleep(150000);
        flock($holder, LOCK_UN);
        fclose($holder);
        fclose($sockets[1]);
        exit(0);
    }
    fclose($sockets[1]);
    waitCheck(fread($sockets[0], 1) === 'L', 'Child must hold the directory before begin');
    $box = new DeliveryOutbox($home . '/concurrent/delivery-outbox.json',
        new NativePrivateStateFilesystem(accountResolver: $account));
    $token = null;
    try { $token = $box->begin(hash('sha256', 'waiting'), ['operator@example.invalid'], 'Title', 'Text'); }
    catch (RuntimeException) { }
    pcntl_waitpid($pid, $status);
    fclose($sockets[0]);
    waitCheck(pcntl_wexitstatus($status) === 0 && is_string($token),
        'Normal concurrent directory contention must wait and persist the outbox claim');
    waitCheck(count($box->listMetadata()['items']) === 1, 'Exactly one claim must persist');

    foreach (['timeout', 'directory', 'ancestor', 'sidecar', 'file_timeout'] as $scenario) {
        $root = $home . '/' . $scenario;
        mkdir($root, 0700);
        mkdir($root . '/state', 0700);
        $directory = $root . '/state';
        $lockPath = $directory . '/.state.lock';
        file_put_contents($lockPath, '');
        chmod($lockPath, 0600);
        $heldPath = $scenario === 'file_timeout' ? $lockPath : $directory;
        $holder = fopen($heldPath, 'rb');
        waitCheck(is_resource($holder) && flock($holder, LOCK_EX), 'Test holder must acquire');
        $time = 100.0;
        $sleeps = 0;
        $callback = false;
        $fs = new NativePrivateStateFilesystem(
            accountResolver: $account,
            monotonicClock: static function () use (&$time): float { return $time; },
            lockSleeper: static function (int $microseconds) use (
                &$time, &$sleeps, $scenario, $directory, $root, $lockPath, $holder,
            ): void {
                $time += $microseconds / 1000000;
                ++$sleeps;
                if ($sleeps !== 1) { return; }
                if ($scenario === 'directory') {
                    rename($directory, $root . '/moved'); mkdir($directory, 0700);
                } elseif ($scenario === 'ancestor') {
                    rename($root, $root . '-moved'); mkdir($root, 0700); mkdir($directory, 0700);
                } elseif ($scenario === 'sidecar') {
                    rename($lockPath, $lockPath . '.old');
                    file_put_contents($lockPath, ''); chmod($lockPath, 0600);
                    flock($holder, LOCK_UN);
                }
            },
            lockWaitSeconds: 0.15,
        );
        waitRefuses(static function () use ($fs, $lockPath, &$callback, $directory): void {
            $fs->withExclusiveLock($lockPath, static function () use ($fs, &$callback, $directory): void {
                $callback = true;
                $fs->replaceAtomic($directory . '/unexpected.json', '{}', 0600);
            });
        });
        waitCheck(!$callback && !file_exists($directory . '/unexpected.json'),
            $scenario . ' must refuse before any callback or write');
        waitCheck($sleeps > 0 && $time <= 100.151,
            $scenario . ' must be bounded, not immediate refusal or unbounded blocking');
        if (in_array($scenario, ['timeout', 'file_timeout'], true)) {
            waitCheck($time >= 100.15, 'Held lock must use the allotted monotonic timeout');
        }
        flock($holder, LOCK_UN); fclose($holder);
    }
    echo "PASS: bounded private-state lock contention and replacement defenses\n";
} finally {
    waitCleanup($home);
}
