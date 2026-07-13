<?php

declare(strict_types=1);

/* Standalone by design: this file must not depend on a release or autoloader. */

function bootstrapFail(): never
{
    fwrite(STDERR, "メール通知を開始できませんでした。管理者へ連絡してください。\n");
    exit(2);
}

function bootstrapUid(): int
{
    if (!function_exists('posix_geteuid')) {
        bootstrapFail();
    }
    return posix_geteuid();
}

/** @param resource $stream */
function bootstrapWriteAll($stream, string $bytes): void
{
    $offset = 0;
    $length = strlen($bytes);
    while ($offset < $length) {
        $written = @fwrite($stream, substr($bytes, $offset));
        if (!is_int($written) || $written < 1) bootstrapFail();
        $offset += $written;
    }
}

/** @param resource $source @param resource $destination */
function bootstrapCopyLimited($source, $destination, int $limit): void
{
    $copied = 0;
    while ($copied < $limit) {
        $chunk = @fread($source, min(8192, $limit - $copied));
        if (!is_string($chunk)) bootstrapFail();
        if ($chunk === '') {
            if (feof($source)) return;
            bootstrapFail();
        }
        bootstrapWriteAll($destination, $chunk);
        $copied += strlen($chunk);
    }
    bootstrapFail();
}

/** @return array<string,mixed> */
function bootstrapStat(string $path, int $mode, string $type): array
{
    $stat = @lstat($path);
    if ($stat === false || (($stat['mode'] & 0170000) !== ($type === 'directory' ? 0040000 : 0100000))) {
        bootstrapFail();
    }
    if ($stat['uid'] !== bootstrapUid() || ($stat['mode'] & 0777) !== $mode) {
        bootstrapFail();
    }
    return $stat;
}

function bootstrapAbsolutePath(string $path): bool
{
    if ($path === '' || $path[0] !== '/') {
        return false;
    }
    foreach (explode('/', $path) as $part) {
        if ($part === '.' || $part === '..' || strcasecmp($part, 'public_html') === 0 || preg_match('/[\x00-\x1f\x7f]/', $part)) {
            return false;
        }
    }
    return true;
}

function bootstrapRelativePath(string $path): bool
{
    return $path !== '' && $path[0] !== '/' && bootstrapAbsolutePath('/' . $path);
}

/** @return array<string,mixed> */
function bootstrapReadJson(string $path): array
{
    $before = bootstrapStat($path, 0600, 'file');
    $handle = @fopen($path, 'rb');
    if ($handle === false) bootstrapFail();
    $after = fstat($handle);
    if ($after === false || $before['dev'] !== $after['dev'] || $before['ino'] !== $after['ino']) {
        fclose($handle); bootstrapFail();
    }
    $body = stream_get_contents($handle, 65537);
    $extra = fgetc($handle);
    fclose($handle);
    if (!is_string($body) || strlen($body) > 65536 || $extra !== false) bootstrapFail();
    try { $value = json_decode($body, true, 16, JSON_THROW_ON_ERROR); } catch (Throwable) { bootstrapFail(); }
    if (!is_array($value) || array_is_list($value)) bootstrapFail();
    return $value;
}

function bootstrapVerifiedBytes(string $path, int $mode, int $size, string $sha256): string
{
    $before = bootstrapStat($path, $mode, 'file');
    $handle = @fopen($path, 'rb');
    if ($handle === false) bootstrapFail();
    $after = fstat($handle);
    if ($after === false || !bootstrapSameIdentity($before, $after)) {
        fclose($handle); bootstrapFail();
    }
    $body = stream_get_contents($handle, $size + 1);
    $final = fstat($handle);
    fclose($handle);
    if (!is_string($body) || strlen($body) !== $size || $final === false
        || !bootstrapSameIdentity($after, $final)
        || !hash_equals($sha256, hash('sha256', $body))) bootstrapFail();
    return $body;
}

/** @param array<string,mixed> $left @param array<string,mixed> $right */
function bootstrapSameIdentity(array $left, array $right): bool
{
    foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'rdev', 'size', 'mtime', 'ctime'] as $key) {
        if (!isset($left[$key], $right[$key]) || $left[$key] !== $right[$key]) return false;
    }
    return true;
}

function bootstrapVerifyIdentity(string $path, int $mode): void
{
    $before = bootstrapStat($path, $mode, 'file');
    $handle = @fopen($path, 'rb');
    if ($handle === false) bootstrapFail();
    $after = fstat($handle); fclose($handle);
    if ($after === false || !bootstrapSameIdentity($before, $after)) bootstrapFail();
}

/** @return array{0:resource,1:string} */
function bootstrapOpenVerified(string $path, int $mode, int $size, string $sha256)
{
    $before = bootstrapStat($path, $mode, 'file');
    $handle = @fopen($path, 'rb');
    if ($handle === false) bootstrapFail();
    $opened = fstat($handle);
    $body = stream_get_contents($handle, $size + 1);
    $final = fstat($handle);
    if ($opened === false || $final === false || !bootstrapSameIdentity($before, $opened)
        || !bootstrapSameIdentity($opened, $final) || !is_string($body)
        || strlen($body) !== $size || !hash_equals($sha256, hash('sha256', $body))) {
        fclose($handle); bootstrapFail();
    }
    if (@rewind($handle) !== true) { fclose($handle); bootstrapFail(); }
    return [$handle, $body];
}

function bootstrapReadConfig(string $path): string
{
    $before = bootstrapStat($path, 0600, 'file');
    if ($before['size'] < 2 || $before['size'] > 65536) bootstrapFail();
    $handle = @fopen($path, 'rb');
    if ($handle === false) bootstrapFail();
    $opened = fstat($handle);
    if ($opened === false || !bootstrapSameIdentity($before, $opened)) { fclose($handle); bootstrapFail(); }
    $body = stream_get_contents($handle, 65537); $final = fstat($handle); fclose($handle);
    if (!is_string($body) || strlen($body) !== $before['size'] || $final === false
        || !bootstrapSameIdentity($opened, $final)) bootstrapFail();
    return $body;
}

/** @return list<string> */
function bootstrapPhpClasses(string $source): array
{
    try { $tokens = token_get_all($source, TOKEN_PARSE); } catch (Throwable) { bootstrapFail(); }
    $namespace = ''; $classes = []; $count = count($tokens);
    for ($i = 0; $i < $count; $i++) {
        $token = $tokens[$i];
        if (!is_array($token)) continue;
        if ($token[0] === T_NAMESPACE) {
            $namespace = '';
            for ($i++; $i < $count; $i++) {
                $part = $tokens[$i];
                if ($part === ';' || $part === '{') break;
                if (is_array($part) && in_array($part[0], [T_STRING, T_NAME_QUALIFIED, T_NS_SEPARATOR], true)) $namespace .= $part[1];
            }
            continue;
        }
        $classTokens = [T_CLASS, T_INTERFACE, T_TRAIT];
        if (defined('T_ENUM')) $classTokens[] = T_ENUM;
        if (!in_array($token[0], $classTokens, true)) continue;
        if ($token[0] === T_CLASS) {
            $j = $i - 1;
            while ($j >= 0 && is_array($tokens[$j]) && in_array($tokens[$j][0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT], true)) $j--;
            if ($j >= 0 && is_array($tokens[$j]) && $tokens[$j][0] === T_DOUBLE_COLON) continue;
        }
        for ($j = $i + 1; $j < $count; $j++) {
            if (is_array($tokens[$j]) && $tokens[$j][0] === T_STRING) {
                $classes[] = ($namespace === '' ? '' : $namespace . '\\') . $tokens[$j][1];
                break;
            }
            if ($tokens[$j] === '{' || $tokens[$j] === '(') break;
        }
    }
    return $classes;
}

$base = dirname(__DIR__);
$locatorPath = $base . '/state/active-release.json';
try {
    $callerArgs = array_slice($argv, 1);
    if ($callerArgs === []) {
        $childArgs = [];
    } elseif (count($callerArgs) === 1 && in_array($callerArgs[0], ['--check-config', '--check-message'], true)) {
        $childArgs = [$callerArgs[0]];
    } else {
        bootstrapFail();
    }
    bootstrapStat($base, 0700, 'directory');
    bootstrapStat(__DIR__, 0700, 'directory');
    bootstrapStat($base . '/state', 0700, 'directory');
    bootstrapStat($base . '/releases', 0700, 'directory');
    bootstrapStat(__FILE__, 0700, 'file');
    $locator = bootstrapReadJson($locatorPath);
    $keys = ['config_path', 'entrypoint', 'manifest_sha256', 'release_id', 'release_path', 'schema_version'];
    $actualKeys = array_keys($locator); sort($actualKeys, SORT_STRING); sort($keys, SORT_STRING);
    if ($actualKeys !== $keys || $locator['schema_version'] !== 1) bootstrapFail();
    foreach (['release_id', 'release_path', 'entrypoint', 'manifest_sha256', 'config_path'] as $key) {
        if (!is_string($locator[$key]) || $locator[$key] === '') bootstrapFail();
    }
    if (!preg_match('/\Arelease-[A-Za-z0-9_-]+\z/', $locator['release_id'])
        || !preg_match('/\A[a-f0-9]{64}\z/', $locator['manifest_sha256'])
        || !bootstrapAbsolutePath($locator['release_path'])
        || !bootstrapAbsolutePath($locator['config_path'])
        || !bootstrapRelativePath($locator['entrypoint'])) bootstrapFail();
    $expectedRelease = $base . '/releases/' . $locator['release_id'];
    if ($locator['release_path'] !== $expectedRelease) bootstrapFail();
    bootstrapStat($expectedRelease, 0700, 'directory');
    $manifestPath = $expectedRelease . '/release-manifest.json';
    $manifestStat = bootstrapStat($manifestPath, 0600, 'file');
    $manifestBody = bootstrapVerifiedBytes($manifestPath, 0600, $manifestStat['size'], $locator['manifest_sha256']);
    try { $manifest = json_decode($manifestBody, true, 8, JSON_THROW_ON_ERROR); } catch (Throwable) { bootstrapFail(); }
    if (!is_array($manifest) || array_is_list($manifest)) bootstrapFail();
    $manifestKeys = array_keys($manifest); sort($manifestKeys, SORT_STRING);
    if ($manifestKeys !== ['entrypoint', 'runtime', 'schema_version'] || $manifest['schema_version'] !== 1
        || !is_array($manifest['entrypoint']) || array_is_list($manifest['entrypoint'])) bootstrapFail();
    $entryKeys = array_keys($manifest['entrypoint']); sort($entryKeys, SORT_STRING);
    if ($entryKeys !== ['mode', 'path', 'sha256', 'size']
        || $manifest['entrypoint']['path'] !== $locator['entrypoint']
        || $manifest['entrypoint']['mode'] !== 0700
        || !is_int($manifest['entrypoint']['size']) || $manifest['entrypoint']['size'] < 1 || $manifest['entrypoint']['size'] > 65536
        || !is_string($manifest['entrypoint']['sha256']) || !preg_match('/\A[a-f0-9]{64}\z/', $manifest['entrypoint']['sha256'])
        || !is_array($manifest['runtime']) || !array_is_list($manifest['runtime']) || count($manifest['runtime']) > 512) bootstrapFail();
    $cursor = $expectedRelease;
    $parts = explode('/', $locator['entrypoint']);
    foreach ($parts as $index => $part) {
        $cursor .= '/' . $part;
        bootstrapStat($cursor, $index === count($parts) - 1 ? 0700 : 0700, $index === count($parts) - 1 ? 'file' : 'directory');
    }
    [$entryHandle, $entryCode] = bootstrapOpenVerified($cursor, 0700, $manifest['entrypoint']['size'], $manifest['entrypoint']['sha256']);
    if (!preg_match('/\A<\?php(?:\s+|$)/', $entryCode)) bootstrapFail();

    $seenPaths = []; $verifiedFiles = [];
    $verifiedFiles[$cursor] = [$entryHandle, $manifest['entrypoint']];
    foreach ($manifest['runtime'] as $record) {
        if (!is_array($record) || array_is_list($record)) bootstrapFail();
        $recordKeys = array_keys($record); sort($recordKeys, SORT_STRING);
        if ($recordKeys !== ['mode', 'path', 'preload', 'sha256', 'size']
            || !is_string($record['path']) || !bootstrapRelativePath($record['path']) || isset($seenPaths[$record['path']])
            || !is_int($record['mode']) || $record['mode'] !== 0600
            || !is_int($record['size']) || $record['size'] < 1 || $record['size'] > 1048576
            || !is_string($record['sha256']) || !preg_match('/\A[a-f0-9]{64}\z/', $record['sha256'])
            || !is_bool($record['preload'])) bootstrapFail();
        $seenPaths[$record['path']] = true;
        $runtimePath = $expectedRelease . '/' . $record['path'];
        $runtimeCursor = $expectedRelease;
        $runtimeParts = explode('/', $record['path']);
        foreach ($runtimeParts as $runtimeIndex => $runtimePart) {
            $runtimeCursor .= '/' . $runtimePart;
            bootstrapStat($runtimeCursor, $runtimeIndex === count($runtimeParts) - 1 ? 0600 : 0700,
                $runtimeIndex === count($runtimeParts) - 1 ? 'file' : 'directory');
        }
        [$runtimeHandle, $runtimeBody] = bootstrapOpenVerified($runtimePath, 0600, $record['size'], $record['sha256']);
        if (!preg_match('/\A<\?php(?:\s+|$)/', $runtimeBody)) bootstrapFail();
        $verifiedFiles[$runtimePath] = [$runtimeHandle, $record];
    }
    $frameCapable = isset($seenPaths['src/StdinFrame.php']);
    $configBody = bootstrapReadConfig($locator['config_path']);
    $environment = getenv();
    if (!is_array($environment)) $environment = [];
    unset($environment['MAIL_NOTIFIER_FD_RUNTIME']);
    unset($environment['MAIL_NOTIFIER_CONFIG_FD'], $environment['MAIL_NOTIFIER_CONFIG']);
    $configFd = null;
    if ($frameCapable) {
        $frame = tmpfile();
        if ($frame === false) bootstrapFail();
        $frameStat = fstat($frame);
        if ($frameStat === false || ($frameStat['mode'] & 0170000) !== 0100000
            || $frameStat['uid'] !== bootstrapUid() || ($frameStat['mode'] & 0777) !== 0600) bootstrapFail();
        bootstrapWriteAll($frame, "XSERVER-MAIL-FRAME\0\x01" . pack('NN', 0, strlen($configBody)) . $configBody);
        bootstrapCopyLimited(STDIN, $frame, 10485761);
        if (fflush($frame) !== true || rewind($frame) !== true) bootstrapFail();
        $descriptors = [0 => $frame, 1 => STDOUT, 2 => STDERR];
        $environment['MAIL_NOTIFIER_STDIN_FRAME'] = '1';
    } else {
        $configFd = 3;
        $descriptors = [0 => STDIN, 1 => STDOUT, 2 => STDERR, $configFd => ['pipe', 'r']];
        $environment['MAIL_NOTIFIER_CONFIG_FD'] = (string) $configFd;
        unset($environment['MAIL_NOTIFIER_STDIN_FRAME']);
    }
    // PHP/Composer must see real paths so __DIR__ remains correct. All release
    // files stay open and are re-read immediately before spawn. A same-UID
    // attacker can never be excluded completely without OS-level immutability;
    // owner-only 0700/0600 permissions and this final identity/hash pass narrow
    // that race to the proc_open boundary and fail closed when observed.
    foreach ($verifiedFiles as $path => [$handle, $record]) {
        $current = bootstrapStat($path, $record['mode'], 'file');
        $opened = fstat($handle);
        if ($opened === false || !bootstrapSameIdentity($opened, $current)) bootstrapFail();
        $body = stream_get_contents($handle, $record['size'] + 1, 0);
        $final = fstat($handle);
        if (!is_string($body) || strlen($body) !== $record['size'] || $final === false
            || !bootstrapSameIdentity($opened, $final)
            || !hash_equals($record['sha256'], hash('sha256', $body))) bootstrapFail();
    }
    $process = proc_open([PHP_BINARY, $cursor, ...$childArgs], $descriptors, $pipes, dirname($cursor), $environment);
    if (!is_resource($process)) bootstrapFail();
    if ($configFd !== null) {
        bootstrapWriteAll($pipes[$configFd], $configBody);
        fclose($pipes[$configFd]);
    }
    $status = proc_close($process);
    exit(is_int($status) ? $status : 2);
} catch (Throwable) {
    bootstrapFail();
}
