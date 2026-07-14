<?php
declare(strict_types=1);
function vrFail(string $message): never { fwrite(STDERR, "FAIL: {$message}\n"); exit(1); }
function vrRemove(string $path): void { if (!is_dir($path) || is_link($path)) { @unlink($path); return; } foreach (array_diff(scandir($path) ?: [], ['.', '..']) as $name) vrRemove($path . '/' . $name); @rmdir($path); }
$source = dirname(__DIR__, 2);
$home = sys_get_temp_dir() . '/validate-entry-' . bin2hex(random_bytes(8));
$release = $home . '/release';
mkdir($release . '/bin', 0700, true);
register_shutdown_function(static fn() => vrRemove($home));
copy($source . '/bin/mail-to-lineworks.php', $release . '/bin/mail-to-lineworks.php');
chmod($release . '/bin/mail-to-lineworks.php', 0700);
foreach (['src', 'vendor'] as $tree) {
    mkdir($release . '/' . $tree, 0700, true); chmod($release . '/' . $tree, 0700);
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($source . '/' . $tree, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::SELF_FIRST);
    foreach ($iterator as $item) {
        $relative = substr($item->getPathname(), strlen($source) + 1);
        $target = $release . '/' . $relative;
        if ($item->isLink()) vrFail('fixture contains symlink');
        if ($item->isDir()) { mkdir($target, 0700, true); chmod($target, 0700); }
        else { copy($item->getPathname(), $target); chmod($target, 0600); }
    }
}
$entries = [];
$iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($release, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::SELF_FIRST);
foreach ($iterator as $item) {
    $path = substr($item->getPathname(), strlen($release) + 1);
    $file = $item->isFile();
    $entries[] = ['path' => $path, 'type' => $file ? 'file' : 'directory',
        'mode' => $file ? ($path === 'bin/mail-to-lineworks.php' ? 0700 : 0600) : 0700,
        'size' => $file ? $item->getSize() : 0,
        'sha256' => $file ? hash_file('sha256', $item->getPathname()) : null];
}
usort($entries, static fn(array $a, array $b): int => strcmp($a['path'], $b['path']));
$entriesByPath = [];
foreach ($entries as $entry) $entriesByPath[$entry['path']] = $entry;
foreach ([
    'CanonicalEmail.php', 'SystemMailAuthenticator.php', 'SystemAlertFormatter.php', 'SendmailClient.php',
    'SendmailProcessAdapter.php', 'NativeSendmailProcessAdapter.php',
    'DeliveryHealthMonitor.php', 'PrivateStateFilesystem.php',
    'NativePrivateStateFilesystem.php',
] as $name) {
    $path = 'src/' . $name;
    if (!isset($entriesByPath[$path]) || $entriesByPath[$path]['mode'] !== 0600
        || $entriesByPath[$path]['sha256'] !== hash_file('sha256', $source . '/' . $path)) {
        vrFail($path . ' is not packaged with exact private metadata');
    }
}
$config = $home . '/config.json';
file_put_contents($config, json_encode(['webhook_url' => 'https://webhook.worksmobile.com/message/test-placeholder',
    'error_recipients' => ['operator@example.invalid'], 'log_path' => $home . '/notifier.log',
    'dedup_path' => $home . '/dedup.json', 'soft_cap_bytes' => 32768,
    'notification_pinned_targets' => [], 'notification_targets' => [],
    'system_mail_hmac_key' => 'dGVzdC1vbmx5LW5vbi1zZWNyZXQta2V5LTMyYnl0ZXM'], JSON_THROW_ON_ERROR));
chmod($config, 0600);
$request = json_encode(['schema_version' => 1, 'release_path' => $release,
    'entrypoint' => 'bin/mail-to-lineworks.php', 'config_path' => $config, 'manifest' => $entries], JSON_THROW_ON_ERROR);
$process = proc_open([PHP_BINARY, $source . '/bin/validate-release.php'],
    [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
if (!is_resource($process)) vrFail('validator did not start');
fwrite($pipes[0], $request); fclose($pipes[0]);
$stdout = stream_get_contents($pipes[1]); fclose($pipes[1]);
$stderr = stream_get_contents($pipes[2]); fclose($pipes[2]);
$status = proc_close($process);
if ($status !== 0 || $stderr !== '') vrFail('real release parser dry-run failed: ' . trim($stderr) . ' status=' . $status);
$result = json_decode($stdout, true, 16, JSON_THROW_ON_ERROR);
if (($result['absolute_cli_dry_run'] ?? null) !== 'PASS') vrFail('dry-run result missing');
fwrite(STDOUT, "validate-release entrypoint tests passed\n");
