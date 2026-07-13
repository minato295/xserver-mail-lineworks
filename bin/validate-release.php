<?php

declare(strict_types=1);

use XserverMail\ReleaseValidator;

require dirname(__DIR__) . '/vendor/autoload.php';

/** @param array<string,mixed> $left @param array<string,mixed> $right */
function validationSameIdentity(array $left, array $right): bool
{
    foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'rdev', 'size', 'mtime', 'ctime'] as $key) {
        if (!isset($left[$key], $right[$key]) || $left[$key] !== $right[$key]) return false;
    }
    return true;
}

/** Revalidate exact manifest bytes and execute the real Composer-based parser. */
function validationMessageDryRun(array $request): void
{
    $root = rtrim($request['release_path'], '/');
    foreach ($request['manifest'] as $record) {
        $path = $root . '/' . $record['path'];
        $before = @lstat($path);
        if ($before === false || ($before['mode'] & 0777) !== $record['mode']
            || (function_exists('posix_geteuid') && $before['uid'] !== posix_geteuid())) throw new RuntimeException('changed');
        if ($record['type'] === 'directory') {
            if (($before['mode'] & 0170000) !== 0040000) throw new RuntimeException('changed');
            continue;
        }
        if (($before['mode'] & 0170000) !== 0100000) throw new RuntimeException('changed');
        $handle = @fopen($path, 'rb');
        if ($handle === false) throw new RuntimeException('changed');
        $opened = fstat($handle); $body = stream_get_contents($handle, $record['size'] + 1); $final = fstat($handle); fclose($handle);
        if ($opened === false || $final === false || !validationSameIdentity($before, $opened)
            || !validationSameIdentity($opened, $final) || !is_string($body)
            || strlen($body) !== $record['size'] || !hash_equals($record['sha256'], hash('sha256', $body))) {
            throw new RuntimeException('changed');
        }
    }

    $configBefore = @lstat($request['config_path']);
    $config = @fopen($request['config_path'], 'rb');
    $configOpened = is_resource($config) ? fstat($config) : false;
    if ($configBefore === false || !is_resource($config) || $configOpened === false
        || ($configBefore['mode'] & 0170000) !== 0100000 || ($configBefore['mode'] & 0777) !== 0600
        || !validationSameIdentity($configBefore, $configOpened)) throw new RuntimeException('config');
    $raw = "From: validator@example.invalid\r\nTo: receiver@example.invalid\r\nDate: Sat, 01 Jan 2000 00:00:00 +0900\r\nMessage-ID: <validator@example.invalid>\r\nSubject: validator\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nvalidator\r\n";
    $configJson = stream_get_contents($config, 65537);
    if (!is_string($configJson) || strlen($configJson) > 65536) throw new RuntimeException('config');
    $message = @tmpfile();
    $frame = "XSERVER-MAIL-FRAME\0\x01" . pack('NN', 0, strlen($configJson)) . $configJson . $raw;
    if ($message === false) throw new RuntimeException('message');
    $messageStat = fstat($message);
    if ($messageStat === false || ($messageStat['mode'] & 0170000) !== 0100000
        || ($messageStat['mode'] & 0777) !== 0600
        || (function_exists('posix_geteuid') && $messageStat['uid'] !== posix_geteuid())) throw new RuntimeException('message');
    $offset = 0;
    while ($offset < strlen($frame)) {
        $written = @fwrite($message, substr($frame, $offset));
        if (!is_int($written) || $written <= 0) throw new RuntimeException('message');
        $offset += $written;
    }
    if (fflush($message) !== true || rewind($message) !== true) throw new RuntimeException('message');
    $environment = getenv(); if (!is_array($environment)) $environment = [];
    $environment['MAIL_NOTIFIER_STDIN_FRAME'] = '1';
    unset($environment['MAIL_NOTIFIER_CONFIG'], $environment['MAIL_NOTIFIER_CONFIG_FD'],
        $environment['MAIL_NOTIFIER_FD_RUNTIME']);
    // Real paths are necessary for Composer __DIR__. Owner-only 0700/0600 and
    // this immediate full revalidation minimize (but cannot eliminate) a
    // same-UID replacement race at the proc_open boundary.
    $entry = $root . '/' . $request['entrypoint'];
    $process = proc_open([PHP_BINARY, $entry, '--check-message'],
        [0 => $message, 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, dirname($entry), $environment);
    if (!is_resource($process)) throw new RuntimeException('spawn');
    $stdout = stream_get_contents($pipes[1], 4097); fclose($pipes[1]);
    $stderr = stream_get_contents($pipes[2], 4097); fclose($pipes[2]);
    $status = proc_close($process); fclose($message); fclose($config);
    if ($status !== 0 || $stdout !== '' || $stderr !== '') throw new RuntimeException('dry-run');
}

try {
    $body = stream_get_contents(STDIN, 1048577);
    if (!is_string($body) || strlen($body) > 1048576) throw new RuntimeException('input');
    $request = json_decode($body, true, 64, JSON_THROW_ON_ERROR);
    if (!is_array($request) || array_is_list($request)) throw new RuntimeException('input');
    $validator = new ReleaseValidator();
    if (in_array('--audit-public-root', $argv, true)) {
        if (array_keys($request) !== ['server_home', 'known_basenames'] || !is_string($request['server_home']) || !is_array($request['known_basenames'])) throw new RuntimeException('input');
        $result = $validator->auditPublicRoot($request['server_home'], $request['known_basenames']);
    } else {
        $result = $validator->validate($request);
        validationMessageDryRun($request);
    }
    fwrite(STDOUT, json_encode($result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . "\n");
    exit(0);
} catch (Throwable) {
    fwrite(STDERR, "REMOTE_VALIDATION_FAILED\n");
    exit(2);
}
