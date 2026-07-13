<?php

declare(strict_types=1);

namespace XserverMail;

use FilesystemIterator;
use RecursiveDirectoryIterator;
use RecursiveIteratorIterator;
use RuntimeException;
use Throwable;

final class ReleaseValidator
{
    private const KEYS = ['config_path', 'entrypoint', 'manifest', 'release_path', 'schema_version'];
    private const RESULT_KEYS = ['absolute_cli_dry_run', 'autoload', 'manifest', 'php_cli', 'schema_version', 'symlinks'];

    /** @internal Deterministic pre-spawn race injection for contract tests only. */
    public function __construct(private readonly ?\Closure $beforeSpawn = null) {}

    /** @param array<string,mixed> $request @return array<string,mixed> */
    public function validate(array $request): array
    {
        $keys = array_keys($request); sort($keys, SORT_STRING);
        $expected = self::KEYS; sort($expected, SORT_STRING);
        if ($keys !== $expected || $request['schema_version'] !== 1
            || !is_string($request['release_path']) || !$this->absolutePath($request['release_path'])
            || !is_string($request['config_path']) || !$this->absolutePath($request['config_path'])
            || !is_string($request['entrypoint']) || !$this->relativePath($request['entrypoint'])
            || !is_array($request['manifest']) || !array_is_list($request['manifest'])) {
            throw new RuntimeException('RELEASE_SCHEMA_INVALID');
        }
        $root = rtrim($request['release_path'], '/');
        $this->assertNode($root, 'directory', 0700);
        $expectedEntries = $this->manifestMap($request['manifest']);
        $actualEntries = $this->walk($root);
        if (array_keys($expectedEntries) !== array_keys($actualEntries)) throw new RuntimeException('RELEASE_MANIFEST_MISMATCH');
        foreach ($expectedEntries as $path => $expectedEntry) {
            if ($expectedEntry !== $actualEntries[$path]) throw new RuntimeException('RELEASE_MANIFEST_MISMATCH');
        }
        $entry = $root . '/' . $request['entrypoint'];
        if (!isset($actualEntries[$request['entrypoint']]) || $actualEntries[$request['entrypoint']]['mode'] !== 0700) {
            throw new RuntimeException('RELEASE_ENTRYPOINT_INVALID');
        }
        if (!isset($actualEntries['vendor/autoload.php'])) throw new RuntimeException('RELEASE_AUTOLOAD_INVALID');
        $requiredExtensions = ['json', 'mbstring', 'curl', 'openssl'];
        foreach ($requiredExtensions as $extension) if (!extension_loaded($extension)) throw new RuntimeException('RELEASE_PHP_INVALID');
        $this->checkAutoload($root, $actualEntries, $request['entrypoint']);
        $configHandle = $this->openConfig($request['config_path']);
        [$command, $descriptors, $handles, $environment] = $this->fdRuntime(
            $root, $actualEntries, $request['entrypoint'], true, $configHandle,
        );
        if ($this->beforeSpawn !== null) ($this->beforeSpawn)('entry');
        $this->revalidateTree($root, $expectedEntries);
        $process = proc_open([...$command, '--check-message'], $descriptors, $pipes, dirname($entry), $environment);
        if (!is_resource($process)) throw new RuntimeException('RELEASE_CLI_INVALID');
        $stdout = stream_get_contents($pipes[1], 4097); fclose($pipes[1]);
        $stderr = stream_get_contents($pipes[2], 4097); fclose($pipes[2]);
        $code = proc_close($process); foreach ($handles as $handle) fclose($handle);
        if ($code !== 0 || $stdout !== '' || $stderr !== '' || $this->looksSecret((string) $stdout . (string) $stderr)) {
            throw new RuntimeException('RELEASE_CLI_INVALID');
        }
        return ['schema_version' => 1, 'manifest' => 'PASS', 'php_cli' => 'PASS', 'autoload' => 'PASS', 'absolute_cli_dry_run' => 'PASS', 'symlinks' => 0];
    }

    /** @param list<string> $knownBasenames @return array<string,mixed> */
    public function auditPublicRoot(string $serverHome, array $knownBasenames): array
    {
        if (!function_exists('posix_geteuid')) throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        if (!$this->absolutePath($serverHome)) throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        $home = rtrim($serverHome, '/');
        $homeStat = $this->auditDirectory($home);
        $known = [];
        foreach ($knownBasenames as $basename) {
            if (!is_string($basename) || $basename === '' || $basename === '.' || $basename === '..'
                || str_contains($basename, '/') || str_contains($basename, "\0") || isset($known[$basename])) {
                throw new RuntimeException('PUBLIC_AUDIT_INVALID');
            }
            $known[$basename] = true;
        }

        $roots = 0; $matches = 0; $symlinks = 0; $untrustedSubtrees = 0; $untrustedEntries = 0;
        $children = @scandir($home);
        if ($children === false) throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        foreach ($children as $name) {
            if ($name === '.' || $name === '..') continue;
            $child = $home . '/' . $name;
            $stat = $this->lstat($child);
            $type = $stat['mode'] & 0170000;
            if ($type === 0120000) throw new RuntimeException('PUBLIC_AUDIT_INVALID');
            if ($type !== 0040000) continue;
            if ($name === 'public_html') {
                $childIdentity = $this->auditDirectory($child);
                $this->auditTree($child, $childIdentity, $known, $matches, $symlinks,
                    $untrustedSubtrees, $untrustedEntries);
                $roots++;
                continue;
            }
            if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-domain', $child);
            if (!$this->sameIdentity($stat, $this->lstat($child))) {
                throw new RuntimeException('PUBLIC_AUDIT_INVALID');
            }
            $candidate = $child . '/public_html';
            $candidateStat = @lstat($candidate);
            if ($candidateStat === false) {
                if (!$this->sameIdentity($stat, $this->lstat($child))) {
                    throw new RuntimeException('PUBLIC_AUDIT_INVALID');
                }
                continue;
            }
            if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-candidate', $child);
            $childIdentity = $this->auditDirectory($child);
            if (!$this->sameIdentity($stat, $childIdentity)) {
                throw new RuntimeException('PUBLIC_AUDIT_INVALID');
            }
            $candidateIdentity = $this->auditDirectory($candidate);
            if (!$this->sameIdentity($childIdentity, $this->lstat($child))) {
                throw new RuntimeException('PUBLIC_AUDIT_INVALID');
            }
            $this->auditTree($candidate, $candidateIdentity, $known, $matches, $symlinks,
                $untrustedSubtrees, $untrustedEntries);
            $roots++;
        }
        return [
            'schema_version' => 3,
            'home_symlink' => false,
            'home_mode' => $homeStat['mode'] & 0777,
            'public_roots_scanned' => $roots,
            'symlinks' => $symlinks,
            'known_product_matches' => $matches,
            'untrusted_subtrees' => $untrustedSubtrees,
            'untrusted_entries' => $untrustedEntries,
        ];
    }

    /** @return array<string,mixed> */
    private function auditDirectory(string $path): array
    {
        $stat = $this->lstat($path);
        if (($stat['mode'] & 0170000) !== 0040000
            || ($stat['mode'] & 0022) !== 0
            || $stat['uid'] !== posix_geteuid()) {
            throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        }
        return $stat;
    }

    /** @param array<string,mixed> $identity @param array<string,true> $known */
    private function auditTree(string $path, array $identity, array $known, int &$matches,
        int &$symlinks, int &$untrustedSubtrees, int &$untrustedEntries): void
    {
        if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-open', $path);
        if (!$this->sameIdentity($identity, $this->lstat($path))) {
            throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        }
        $iterator = new FilesystemIterator($path, FilesystemIterator::SKIP_DOTS);
        if (!$this->sameIdentity($identity, $this->lstat($path))) {
            throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        }
        foreach ($iterator as $item) {
            if (!$this->sameIdentity($identity, $this->lstat($path))) {
                throw new RuntimeException('PUBLIC_AUDIT_INVALID');
            }
            $child = $item->getPathname();
            $stat = $this->lstat($child);
            $type = $stat['mode'] & 0170000;
            if (isset($known[$item->getBasename()])) $matches++;
            if ($type === 0120000) {
                if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-symlink', $child);
                if (!$this->sameIdentity($stat, $this->lstat($child))) {
                    throw new RuntimeException('PUBLIC_AUDIT_INVALID');
                }
                $symlinks++;
            } elseif (!$this->trustedAuditNode($stat, $type)) {
                if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-untrusted', $child);
                if (!$this->sameIdentity($stat, $this->lstat($child))) {
                    throw new RuntimeException('PUBLIC_AUDIT_INVALID');
                }
                if ($type === 0040000) $untrustedSubtrees++; else $untrustedEntries++;
            } elseif ($type === 0040000) {
                if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-descend', $child);
                $this->auditTree($child, $stat, $known, $matches, $symlinks,
                    $untrustedSubtrees, $untrustedEntries);
                if (!$this->sameIdentity($stat, $this->lstat($child))
                    || !$this->sameIdentity($identity, $this->lstat($path))) {
                    throw new RuntimeException('PUBLIC_AUDIT_INVALID');
                }
            } else {
                if ($this->beforeSpawn !== null) ($this->beforeSpawn)('audit-file', $child);
                if (!$this->sameIdentity($stat, $this->lstat($child))) {
                    throw new RuntimeException('PUBLIC_AUDIT_INVALID');
                }
            }
        }
        if (!$this->sameIdentity($identity, $this->lstat($path))) {
            throw new RuntimeException('PUBLIC_AUDIT_INVALID');
        }
    }

    /** @param array<string,mixed> $stat */
    private function trustedAuditNode(array $stat, int $type): bool
    {
        return in_array($type, [0040000, 0100000], true)
            && $stat['uid'] === posix_geteuid()
            && ($stat['mode'] & 0022) === 0;
    }

    /** @param list<mixed> $manifest @return array<string,array<string,mixed>> */
    private function manifestMap(array $manifest): array
    {
        $map = [];
        foreach ($manifest as $entry) {
            if (!is_array($entry) || array_is_list($entry)) throw new RuntimeException('RELEASE_SCHEMA_INVALID');
            $keys = array_keys($entry); sort($keys, SORT_STRING);
            if ($keys !== ['mode', 'path', 'sha256', 'size', 'type'] || !is_string($entry['path']) || !$this->relativePath($entry['path'])
                || !in_array($entry['type'], ['directory', 'file'], true) || !is_int($entry['mode']) || !is_int($entry['size'])) {
                throw new RuntimeException('RELEASE_SCHEMA_INVALID');
            }
            if ($entry['type'] === 'directory') {
                if ($entry['mode'] !== 0700 || $entry['size'] !== 0 || $entry['sha256'] !== null) throw new RuntimeException('RELEASE_SCHEMA_INVALID');
            } elseif (!is_string($entry['sha256']) || !preg_match('/\A[a-f0-9]{64}\z/', $entry['sha256']) || !in_array($entry['mode'], [0600, 0700], true) || $entry['size'] < 0) {
                throw new RuntimeException('RELEASE_SCHEMA_INVALID');
            }
            if (isset($map[$entry['path']])) throw new RuntimeException('RELEASE_SCHEMA_INVALID');
            $map[$entry['path']] = ['type' => $entry['type'], 'mode' => $entry['mode'], 'size' => $entry['size'], 'sha256' => $entry['sha256']];
        }
        ksort($map, SORT_STRING); return $map;
    }

    /** @return array<string,array<string,mixed>> */
    private function walk(string $root): array
    {
        $map = [];
        $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::SELF_FIRST);
        foreach ($iterator as $item) {
            $absolute = $item->getPathname();
            $relative = substr($absolute, strlen($root) + 1);
            $stat = $this->lstat($absolute);
            if (($stat['mode'] & 0170000) === 0120000) throw new RuntimeException('RELEASE_SYMLINK_INVALID');
            $type = (($stat['mode'] & 0170000) === 0040000) ? 'directory' : ((($stat['mode'] & 0170000) === 0100000) ? 'file' : 'other');
            if ($type === 'other') throw new RuntimeException('RELEASE_TYPE_INVALID');
            $mode = $stat['mode'] & 0777;
            if (($type === 'directory' && $mode !== 0700) || ($type === 'file' && !in_array($mode, [0600, 0700], true))) throw new RuntimeException('RELEASE_MODE_INVALID');
            if (function_exists('posix_geteuid') && $stat['uid'] !== posix_geteuid()) throw new RuntimeException('RELEASE_OWNER_INVALID');
            $map[$relative] = ['type' => $type, 'mode' => $mode, 'size' => $type === 'file' ? $stat['size'] : 0, 'sha256' => $type === 'file' ? $this->safeHash($absolute, $stat) : null];
        }
        ksort($map, SORT_STRING); return $map;
    }

    /** @param array<string,mixed> $before */
    private function safeHash(string $path, array $before): string
    {
        $handle = @fopen($path, 'rb'); if ($handle === false) throw new RuntimeException('RELEASE_FILE_INVALID');
        $after = fstat($handle);
        if ($after === false || !$this->sameIdentity($before, $after)) { fclose($handle); throw new RuntimeException('RELEASE_FILE_CHANGED'); }
        $context = hash_init('sha256'); hash_update_stream($context, $handle);
        $final = fstat($handle);
        if ($final === false || !$this->sameIdentity($after, $final)) { fclose($handle); throw new RuntimeException('RELEASE_FILE_CHANGED'); }
        fclose($handle); return hash_final($context);
    }

    /** @param array<string,array<string,mixed>> $entries */
    private function checkAutoload(string $root, array $entries, string $entrypoint): void
    {
        [$command, $descriptors, $handles, $environment] = $this->fdRuntime($root, $entries, $entrypoint, false);
        $process = proc_open($command, $descriptors, $pipes, null, $environment);
        if (!is_resource($process)) throw new RuntimeException('RELEASE_AUTOLOAD_INVALID');
        $stdout = stream_get_contents($pipes[1], 1025); fclose($pipes[1]);
        $stderr = stream_get_contents($pipes[2], 1025); fclose($pipes[2]);
        $status = proc_close($process); foreach ($handles as $handle) fclose($handle);
        if ($status !== 0 || $stdout !== '' || strlen((string) $stderr) > 1024) throw new RuntimeException('RELEASE_AUTOLOAD_INVALID');
    }

    /** @param array<string,array<string,mixed>> $expected */
    private function revalidateTree(string $root, array $expected): void
    {
        $actual = $this->walk($root);
        if (array_keys($expected) !== array_keys($actual)) throw new RuntimeException('RELEASE_FILE_CHANGED');
        foreach ($expected as $path => $entry) if ($entry !== $actual[$path]) throw new RuntimeException('RELEASE_FILE_CHANGED');
    }

    /** @param array<string,array<string,mixed>> $entries
     *  @param resource|null $config
     *  @return array{0:list<string>,1:array<int,mixed>,2:list<resource>,3:array<string,string>}
     */
    private function fdRuntime(string $root, array $entries, string $entrypoint, bool $executeEntry, $config = null): array
    {
        $descriptors = [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
        $handles = [];
        foreach ($entries as $path => $expected) {
            if ($expected['type'] !== 'file' || !str_ends_with($path, '.php')) continue;
            [$handle, $body] = $this->openVerified($root . '/' . $path, $expected);
            $handles[] = $handle;
        }
        $environment = getenv(); if (!is_array($environment)) $environment = [];
        unset($environment['MAIL_NOTIFIER_FD_RUNTIME'], $environment['MAIL_NOTIFIER_CONFIG'],
            $environment['MAIL_NOTIFIER_CONFIG_FD'], $environment['MAIL_NOTIFIER_STDIN_FRAME']);
        if ($executeEntry) {
            if (!is_resource($config)) throw new RuntimeException('RELEASE_CONFIG_INVALID');
            $raw = "From: validator@example.invalid\r\nTo: receiver@example.invalid\r\nDate: Sat, 01 Jan 2000 00:00:00 +0900\r\nMessage-ID: <validator@example.invalid>\r\nSubject: validator\r\n\r\nvalidator\r\n";
            $frame = $this->stdinFrame($config, $raw);
            $descriptors[0] = $frame; $handles[] = $frame; $handles[] = $config;
            $environment['MAIL_NOTIFIER_STDIN_FRAME'] = '1';
            $command = [PHP_BINARY, $root . '/' . $entrypoint];
        } else {
            $command = [PHP_BINARY, '-r', 'require $argv[1];', '--', $root . '/vendor/autoload.php'];
        }
        return [$command, $descriptors, $handles, $environment];
    }

    /** @param resource $config @return resource */
    private function stdinFrame($config, string $message)
    {
        if (@rewind($config) !== true) throw new RuntimeException('RELEASE_CONFIG_INVALID');
        $configJson = stream_get_contents($config, 65537);
        if (!is_string($configJson) || strlen($configJson) > 65536) throw new RuntimeException('RELEASE_CONFIG_INVALID');
        $frame = @tmpfile();
        if ($frame === false) throw new RuntimeException('RELEASE_CLI_INVALID');
        $stat = fstat($frame);
        if ($stat === false || ($stat['mode'] & 0170000) !== 0100000 || ($stat['mode'] & 0777) !== 0600
            || (function_exists('posix_geteuid') && $stat['uid'] !== posix_geteuid())) {
            fclose($frame); throw new RuntimeException('RELEASE_CLI_INVALID');
        }
        $body = "XSERVER-MAIL-FRAME\0\x01" . pack('NN', 0, strlen($configJson)) . $configJson . $message;
        $offset = 0;
        while ($offset < strlen($body)) {
            $written = @fwrite($frame, substr($body, $offset));
            if (!is_int($written) || $written <= 0) {
                fclose($frame); throw new RuntimeException('RELEASE_CLI_INVALID');
            }
            $offset += $written;
        }
        if (@fflush($frame) !== true || @rewind($frame) !== true) {
            fclose($frame); throw new RuntimeException('RELEASE_CLI_INVALID');
        }
        return $frame;
    }

    /** @param array<string,mixed> $expected @return array{0:resource,1:string} */
    private function openVerified(string $path, array $expected): array
    {
        $before = $this->lstat($path); $handle = @fopen($path, 'rb');
        if ($handle === false) throw new RuntimeException('RELEASE_FILE_INVALID');
        $opened = fstat($handle); $body = stream_get_contents($handle, $expected['size'] + 1); $final = fstat($handle);
        if ($opened === false || $final === false || !$this->sameIdentity($before, $opened) || !$this->sameIdentity($opened, $final)
            || !is_string($body) || strlen($body) !== $expected['size'] || !hash_equals($expected['sha256'], hash('sha256', $body))
            || @rewind($handle) !== true) { fclose($handle); throw new RuntimeException('RELEASE_FILE_CHANGED'); }
        return [$handle, $body];
    }

    /** @return resource */
    private function openConfig(string $path)
    {
        $before = $this->lstat($path);
        if (($before['mode'] & 0170000) !== 0100000 || ($before['mode'] & 0777) !== 0600
            || (function_exists('posix_geteuid') && $before['uid'] !== posix_geteuid())
            || $before['size'] < 0 || $before['size'] > 65536) {
            throw new RuntimeException('RELEASE_CONFIG_INVALID');
        }
        if ($this->beforeSpawn !== null) ($this->beforeSpawn)('config-open', $path);
        $source = @fopen($path, 'rb');
        if ($source === false) throw new RuntimeException('RELEASE_CONFIG_INVALID');
        $opened = fstat($source); $body = stream_get_contents($source, 65537); $final = fstat($source);
        if ($opened === false || $final === false || !$this->sameIdentity($before, $opened)
            || !$this->sameIdentity($opened, $final) || !is_string($body)
            || strlen($body) !== $opened['size'] || strlen($body) > 65536) {
            fclose($source); throw new RuntimeException('RELEASE_CONFIG_INVALID');
        }
        fclose($source);

        $fault = $this->beforeSpawn !== null ? ($this->beforeSpawn)('config-snapshot') : null;
        if ($fault === 'fail') throw new RuntimeException('RELEASE_CONFIG_INVALID');
        $snapshot = @tmpfile();
        if ($snapshot === false) throw new RuntimeException('RELEASE_CONFIG_INVALID');
        if ($fault === 'bad-mode') {
            $metadata = stream_get_meta_data($snapshot);
            if (isset($metadata['uri']) && is_string($metadata['uri'])) @chmod($metadata['uri'], 0640);
        }
        $initial = fstat($snapshot);
        if ($initial === false || ($initial['mode'] & 0170000) !== 0100000
            || ($initial['mode'] & 0777) !== 0600
            || (function_exists('posix_geteuid') && $initial['uid'] !== posix_geteuid())) {
            fclose($snapshot); throw new RuntimeException('RELEASE_CONFIG_INVALID');
        }
        $offset = 0; $length = strlen($body);
        while ($offset < $length) {
            $written = $fault === 'short-write' ? 0 : @fwrite($snapshot, substr($body, $offset));
            $fault = null;
            if (!is_int($written) || $written <= 0) {
                fclose($snapshot); throw new RuntimeException('RELEASE_CONFIG_INVALID');
            }
            $offset += $written;
        }
        if (@fflush($snapshot) !== true || @rewind($snapshot) !== true) {
            fclose($snapshot); throw new RuntimeException('RELEASE_CONFIG_INVALID');
        }
        $copied = stream_get_contents($snapshot, 65537); $snapStat = fstat($snapshot);
        if (!is_string($copied) || !hash_equals(hash('sha256', $body), hash('sha256', $copied))
            || $copied !== $body || $snapStat === false || ($snapStat['mode'] & 0170000) !== 0100000
            || ($snapStat['mode'] & 0777) !== 0600 || $snapStat['size'] !== $length
            || (function_exists('posix_geteuid') && $snapStat['uid'] !== posix_geteuid())
            || @rewind($snapshot) !== true) {
            fclose($snapshot); throw new RuntimeException('RELEASE_CONFIG_INVALID');
        }
        return $snapshot;
    }

    /** @return list<string> */
    private function phpClasses(string $source): array
    {
        try { $tokens = token_get_all($source, TOKEN_PARSE); } catch (Throwable) { throw new RuntimeException('RELEASE_AUTOLOAD_INVALID'); }
        $namespace = ''; $classes = []; $count = count($tokens);
        for ($i = 0; $i < $count; $i++) {
            $token = $tokens[$i]; if (!is_array($token)) continue;
            if ($token[0] === T_NAMESPACE) {
                $namespace = '';
                for ($i++; $i < $count; $i++) { $part = $tokens[$i]; if ($part === ';' || $part === '{') break;
                    if (is_array($part) && in_array($part[0], [T_STRING, T_NAME_QUALIFIED, T_NS_SEPARATOR], true)) $namespace .= $part[1]; }
                continue;
            }
            $types = [T_CLASS, T_INTERFACE, T_TRAIT]; if (defined('T_ENUM')) $types[] = T_ENUM;
            if (!in_array($token[0], $types, true)) continue;
            if ($token[0] === T_CLASS) { $j = $i - 1; while ($j >= 0 && is_array($tokens[$j]) && in_array($tokens[$j][0], [T_WHITESPACE,T_COMMENT,T_DOC_COMMENT], true)) $j--;
                if ($j >= 0 && is_array($tokens[$j]) && $tokens[$j][0] === T_DOUBLE_COLON) continue; }
            for ($j = $i + 1; $j < $count; $j++) { if (is_array($tokens[$j]) && $tokens[$j][0] === T_STRING) { $classes[] = ($namespace === '' ? '' : $namespace . '\\') . $tokens[$j][1]; break; }
                if ($tokens[$j] === '{' || $tokens[$j] === '(') break; }
        }
        return $classes;
    }

    /** @param array<string,mixed> $left @param array<string,mixed> $right */
    private function sameIdentity(array $left, array $right): bool
    {
        foreach (['dev', 'ino', 'mode', 'nlink', 'uid', 'gid', 'rdev', 'size', 'mtime', 'ctime'] as $key) {
            if (!array_key_exists($key, $left) || !array_key_exists($key, $right) || $left[$key] !== $right[$key]) return false;
        }
        return true;
    }

    /** @return array<string,mixed> */
    private function lstat(string $path): array { $stat = @lstat($path); if ($stat === false) throw new RuntimeException('RELEASE_PATH_INVALID'); return $stat; }
    private function assertNode(string $path, string $type, int $mode): void { $stat = $this->lstat($path); $expected = $type === 'directory' ? 0040000 : 0100000; if (($stat['mode'] & 0170000) !== $expected || ($stat['mode'] & 0777) !== $mode || (function_exists('posix_geteuid') && $stat['uid'] !== posix_geteuid())) throw new RuntimeException('RELEASE_PATH_INVALID'); }
    private function absolutePath(string $path): bool { return $path !== '' && $path[0] === '/' && $this->pathPartsValid($path); }
    private function relativePath(string $path): bool { return $path !== '' && $path[0] !== '/' && $this->pathPartsValid('/' . $path); }
    private function pathPartsValid(string $path): bool { foreach (explode('/', $path) as $part) if ($part === '.' || $part === '..' || strcasecmp($part, 'public_html') === 0 || preg_match('/[\x00-\x1f\x7f]/', $part)) return false; return true; }
    private function looksSecret(string $value): bool { return preg_match('~webhook\.worksmobile\.com|\bxs_[A-Za-z0-9_-]+|https?://[^\s]+~i', $value) === 1; }
}
