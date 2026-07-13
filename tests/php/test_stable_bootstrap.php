<?php

declare(strict_types=1);

function fail(string $message): never { fwrite(STDERR, "FAIL: {$message}\n"); exit(1); }
function ok(bool $condition, string $message): void { if (!$condition) fail($message); }
function rrmdir(string $path): void {
    if (!is_dir($path) || is_link($path)) { @unlink($path); return; }
    foreach (array_diff(scandir($path) ?: [], ['.', '..']) as $name) rrmdir($path . '/' . $name);
    @rmdir($path);
}

$source = dirname(__DIR__, 2) . '/bin/stable-mail-entrypoint.php';
ok(is_file($source), 'stable bootstrap is missing');
$root = sys_get_temp_dir() . '/stable-bootstrap-' . bin2hex(random_bytes(8));
register_shutdown_function(static fn() => rrmdir($root));
mkdir($root, 0700, true); chmod($root, 0700);
$root = realpath($root) ?: fail('temporary root could not be canonicalized');
foreach (['bootstrap', 'state', 'releases', 'releases/release-abc', 'releases/release-abc/bin', 'releases/release-abc/src', 'releases/release-abc/vendor', 'private-config'] as $dir) {
    mkdir($root . '/' . $dir, 0700, true);
    chmod($root . '/' . $dir, 0700);
}
copy($source, $root . '/bootstrap/mail-forward-command.php');
chmod($root . '/bootstrap/mail-forward-command.php', 0700);
$target = $root . '/releases/release-abc/bin/mail-to-lineworks.php';
$dependency = $root . '/releases/release-abc/src/Dependency.php';
$frameCapability = $root . '/releases/release-abc/src/StdinFrame.php';
$autoload = $root . '/releases/release-abc/vendor/autoload.php';
file_put_contents($dependency, "<?php\nnamespace RaceFixture; final class Dependency { public static function value(): string { return 'verified'; } }\n");
chmod($dependency, 0600);
file_put_contents($frameCapability, "<?php\nnamespace RaceFixture; final class StdinFrame {}\n");
chmod($frameCapability, 0600);
file_put_contents($autoload, "<?php\nspl_autoload_register(static function(string \$class): void { if (\$class === 'RaceFixture\\\\Dependency') require dirname(__DIR__) . '/src/Dependency.php'; });\n");
chmod($autoload, 0600);
file_put_contents($target, <<<'PHP'
<?php
declare(strict_types=1);
require dirname(__DIR__) . '/vendor/autoload.php';
$legacyConfig = getenv('MAIL_NOTIFIER_CONFIG');
$legacyFd = getenv('MAIL_NOTIFIER_CONFIG_FD');
if ($legacyConfig !== false || $legacyFd !== false) exit(81);
$magic = "XSERVER-MAIL-FRAME\0\x01";
$header = '';
while (strlen($header) < strlen($magic) + 8) {
    $part = fread(STDIN, strlen($magic) + 8 - strlen($header));
    if (!is_string($part) || $part === '') exit(82);
    $header .= $part;
}
$length = unpack('Nhigh/Nlow', substr($header, strlen($magic), 8));
if (substr($header, 0, strlen($magic)) !== $magic || !is_array($length) || $length['high'] !== 0) exit(83);
$configBytes = '';
while (strlen($configBytes) < $length['low']) {
    $part = fread(STDIN, $length['low'] - strlen($configBytes));
    if (!is_string($part) || $part === '') exit(84);
    $configBytes .= $part;
}
$input = stream_get_contents(STDIN);
if (!is_string($input)) exit(85);
$dependency = getenv('RACE_DEPENDENCY_PATH');
$configPath = getenv('RACE_CONFIG_PATH');
if (is_string($dependency) && is_string($configPath)) {
    rename($dependency, $dependency . '.verified');
    file_put_contents($dependency, "<?php namespace RaceFixture; final class Dependency { public static function value(): string { return 'replaced'; } }");
    chmod($dependency, 0600);
    rename($configPath, $configPath . '.verified');
    file_put_contents($configPath, '{"value":"replaced"}'); chmod($configPath, 0600);
}
$capture = getenv('BOOTSTRAP_CAPTURE');
if (is_string($capture) && $capture !== '') {
    file_put_contents($capture, json_encode([
        'argv' => $argv,
        'config_ok' => hash_equals(hash('sha256', '{"value":"verified"}'), hash('sha256', $configBytes)),
        'input_ok' => hash_equals(hash('sha256', "From: test@example.invalid\n\nbody"), hash('sha256', $input)),
        'frame_env' => getenv('MAIL_NOTIFIER_STDIN_FRAME'),
        'dependency' => RaceFixture\Dependency::value(),
        'entry_file' => __FILE__,
    ], JSON_THROW_ON_ERROR));
}
if (is_string($dependency) && is_string($configPath)) {
    unlink($dependency); rename($dependency . '.verified', $dependency);
    unlink($configPath); rename($configPath . '.verified', $configPath);
}
PHP);
chmod($target, 0700);
$config = $root . '/private-config/config.json';
file_put_contents($config, '{"value":"verified"}'); chmod($config, 0600);
$manifest = $root . '/releases/release-abc/release-manifest.json';
$manifestValue = ['schema_version' => 1, 'entrypoint' => [
    'path' => 'bin/mail-to-lineworks.php', 'size' => filesize($target),
    'sha256' => hash_file('sha256', $target), 'mode' => 0700,
], 'runtime' => [[
    'path' => 'src/Dependency.php', 'size' => filesize($dependency),
    'sha256' => hash_file('sha256', $dependency), 'mode' => 0600, 'preload' => false,
], [
    'path' => 'src/StdinFrame.php', 'size' => filesize($frameCapability),
    'sha256' => hash_file('sha256', $frameCapability), 'mode' => 0600, 'preload' => false,
], [
    'path' => 'vendor/autoload.php', 'size' => filesize($autoload),
    'sha256' => hash_file('sha256', $autoload), 'mode' => 0600, 'preload' => false,
]]];
file_put_contents($manifest, json_encode($manifestValue, JSON_THROW_ON_ERROR)); chmod($manifest, 0600);
$locator = [
    'schema_version' => 1,
    'release_id' => 'release-abc',
    'release_path' => $root . '/releases/release-abc',
    'entrypoint' => 'bin/mail-to-lineworks.php',
    'manifest_sha256' => hash_file('sha256', $manifest),
    'config_path' => $config,
];
$locatorPath = $root . '/state/active-release.json';
file_put_contents($locatorPath, json_encode($locator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);
$capture = $root . '/capture.json';
$command = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg($root . '/bootstrap/mail-forward-command.php') . ' 3<&-';
$testEnvironment = getenv();
if (!is_array($testEnvironment)) $testEnvironment = [];
$pipes = [];
$process = proc_open($command, [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $pipes, null, array_replace($testEnvironment, [
    'BOOTSTRAP_CAPTURE' => $capture,
]));
ok(is_resource($process), 'bootstrap process could not start');
fwrite($pipes[0], "From: test@example.invalid\n\nbody"); fclose($pipes[0]);
$stdout = stream_get_contents($pipes[1]); fclose($pipes[1]);
$stderr = stream_get_contents($pipes[2]); fclose($pipes[2]);
$validCode = proc_close($process);
ok($validCode === 0, 'valid bootstrap failed with fixed classification: ' . trim($stderr));
ok($stdout === '' && $stderr === '', 'valid bootstrap emitted output');
$actual = json_decode((string) file_get_contents($capture), true, 8, JSON_THROW_ON_ERROR);
ok($actual['argv'] === [$target], 'bootstrap argv admitted caller-controlled arguments');
ok($actual['config_ok'] === true, 'bootstrap did not frame verified config bytes');
ok($actual['input_ok'] === true, 'bootstrap did not forward stdin after the frame');
ok($actual['frame_env'] === '1', 'bootstrap did not advertise framed stdin');
ok($actual['dependency'] === 'verified', 'runtime dependency was reopened by path after verification');
ok($actual['entry_file'] === $target, 'bootstrap did not execute the real release entrypoint path');
$bootstrapSource = (string) file_get_contents($source);
ok(str_contains($bootstrapSource, '[PHP_BINARY, $cursor, ...$childArgs]'), 'bootstrap does not pass only validated arguments to the verified real release path');
ok(!str_contains($bootstrapSource, 'require "/dev/fd/".$m["entry"]'), 'bootstrap still executes the entrypoint through /dev/fd');
ok(str_contains($bootstrapSource, 'function bootstrapWriteAll('), 'bootstrap has no partial-write-safe frame writer');
ok(str_contains($bootstrapSource, 'function bootstrapCopyLimited('), 'bootstrap has no bounded stdin copier');

$frameTargetBody = (string) file_get_contents($target);
$frameManifestBody = (string) file_get_contents($manifest);
$legacyTargetBody = <<<'PHP'
<?php
declare(strict_types=1);
if (getenv('MAIL_NOTIFIER_STDIN_FRAME') !== false || getenv('MAIL_NOTIFIER_CONFIG') !== false) exit(91);
$fd = getenv('MAIL_NOTIFIER_CONFIG_FD');
if (!is_string($fd) || !preg_match('/\A[3-9][0-9]*\z/', $fd)) exit(92);
$config = file_get_contents('/dev/fd/' . $fd);
$message = stream_get_contents(STDIN);
if ($config !== '{"value":"verified"}' || $message !== "From: legacy@example.invalid\n\nbody") exit(93);
$capture = getenv('BOOTSTRAP_CAPTURE');
if (is_string($capture) && $capture !== '') file_put_contents($capture, 'legacy');
PHP;
file_put_contents($target, $legacyTargetBody); chmod($target, 0700);
$legacyManifestValue = $manifestValue;
$legacyManifestValue['entrypoint']['size'] = strlen($legacyTargetBody);
$legacyManifestValue['entrypoint']['sha256'] = hash('sha256', $legacyTargetBody);
$legacyManifestValue['runtime'] = array_values(array_filter(
    $legacyManifestValue['runtime'],
    static fn(array $record): bool => $record['path'] !== 'src/StdinFrame.php'
));
file_put_contents($manifest, json_encode($legacyManifestValue, JSON_THROW_ON_ERROR)); chmod($manifest, 0600);
$legacyLocator = array_replace($locator, ['manifest_sha256' => hash_file('sha256', $manifest)]);
file_put_contents($locatorPath, json_encode($legacyLocator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);
@unlink($capture);
$legacyPipes = [];
$legacy = proc_open($command, [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $legacyPipes, null,
    array_replace($testEnvironment, ['BOOTSTRAP_CAPTURE' => $capture]));
ok(is_resource($legacy), 'legacy bootstrap process could not start');
fwrite($legacyPipes[0], "From: legacy@example.invalid\n\nbody"); fclose($legacyPipes[0]);
stream_get_contents($legacyPipes[1]); fclose($legacyPipes[1]);
$legacyStderr = stream_get_contents($legacyPipes[2]); fclose($legacyPipes[2]);
ok(proc_close($legacy) === 0, 'verified legacy release did not use config FD: ' . trim($legacyStderr));
ok(file_get_contents($capture) === 'legacy', 'legacy config FD fixture was not executed');
file_put_contents($target, $frameTargetBody); chmod($target, 0700);
file_put_contents($manifest, $frameManifestBody); chmod($manifest, 0600);
file_put_contents($locatorPath, json_encode($locator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);

foreach (['--check-config', '--check-message'] as $allowedArg) {
    @unlink($capture);
    $allowedPipes = [];
    $allowed = proc_open($command . ' ' . escapeshellarg($allowedArg), [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $allowedPipes, null, array_replace($testEnvironment, [
        'BOOTSTRAP_CAPTURE' => $capture,
        'MAIL_NOTIFIER_CONFIG' => '/secret/legacy/config',
        'MAIL_NOTIFIER_CONFIG_FD' => '99',
    ]));
    ok(is_resource($allowed), "bootstrap could not start with {$allowedArg}");
    fwrite($allowedPipes[0], "From: test@example.invalid\n\nbody"); fclose($allowedPipes[0]);
    stream_get_contents($allowedPipes[1]); fclose($allowedPipes[1]);
    $allowedStderr = stream_get_contents($allowedPipes[2]); fclose($allowedPipes[2]);
    ok(proc_close($allowed) === 0, "bootstrap rejected {$allowedArg}: " . trim($allowedStderr));
    $allowedCapture = json_decode((string) file_get_contents($capture), true, 8, JSON_THROW_ON_ERROR);
    ok($allowedCapture['argv'] === [$target, $allowedArg], "bootstrap did not pass {$allowedArg} exactly");
}

foreach ([['--unknown'], ['--check-config', '--check-message'], ['--check-config', '--check-config']] as $rejectedArgs) {
    $rejectedCommand = $command;
    foreach ($rejectedArgs as $arg) $rejectedCommand .= ' ' . escapeshellarg($arg);
    exec($rejectedCommand . ' </dev/null >/dev/null 2>&1', $ignored, $code);
    ok($code !== 0, 'bootstrap accepted caller arguments: ' . implode(' ', $rejectedArgs));
}

$oversizePipes = [];
$oversize = proc_open($command, [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $oversizePipes, null, array_replace($testEnvironment, ['BOOTSTRAP_CAPTURE' => $capture]));
ok(is_resource($oversize), 'oversize bootstrap process could not start');
$remaining = 10485761;
$chunk = str_repeat('x', 65536);
while ($remaining > 0) {
    $written = fwrite($oversizePipes[0], substr($chunk, 0, min(strlen($chunk), $remaining)));
    if ($written === false || $written === 0) break;
    $remaining -= $written;
}
fclose($oversizePipes[0]);
stream_get_contents($oversizePipes[1]); fclose($oversizePipes[1]);
$oversizeStderr = stream_get_contents($oversizePipes[2]); fclose($oversizePipes[2]);
ok(proc_close($oversize) !== 0, 'bootstrap accepted a 10 MiB + 1 message');
ok(!str_contains($oversizeStderr, '{"value":"verified"}') && !str_contains($oversizeStderr, '/secret/legacy/config'), 'bootstrap exposed configuration in an error');

$validManifestBody = file_get_contents($manifest);
foreach ([
    'template text' => "<html><?= \$value ?></html>\n",
    'UTF-8 BOM' => "\xEF\xBB\xBF<?php function invalid_bom(): void {}\n",
    'leading whitespace' => " \n<?php function invalid_leading(): void {}\n",
] as $name => $invalidRuntimeBody) {
    file_put_contents($dependency, $invalidRuntimeBody); chmod($dependency, 0600);
    $invalidRuntimeManifest = $manifestValue;
    $invalidRuntimeManifest['runtime'][0]['size'] = strlen($invalidRuntimeBody);
    $invalidRuntimeManifest['runtime'][0]['sha256'] = hash('sha256', $invalidRuntimeBody);
    file_put_contents($manifest, json_encode($invalidRuntimeManifest, JSON_THROW_ON_ERROR)); chmod($manifest, 0600);
    $invalidRuntimeLocator = array_replace($locator, ['manifest_sha256' => hash_file('sha256', $manifest)]);
    file_put_contents($locatorPath, json_encode($invalidRuntimeLocator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);
    exec($command . ' 2>&1', $ignored, $code);
    ok($code !== 0, "runtime accepted invalid PHP opening: {$name}");
}
file_put_contents($dependency, "<?php\nnamespace RaceFixture; final class Dependency { public static function value(): string { return 'verified'; } }\n");
chmod($dependency, 0600);
file_put_contents($manifest, $validManifestBody); chmod($manifest, 0600);
file_put_contents($locatorPath, json_encode($locator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);

$invalidCases = [
    'unknown key' => $locator + ['unknown' => 'x'],
    'missing key' => array_diff_key($locator, ['entrypoint' => true]),
    'relative release' => array_replace($locator, ['release_path' => 'relative/release']),
    'dot segment' => array_replace($locator, ['entrypoint' => 'bin/../mail-to-lineworks.php']),
    'public html' => array_replace($locator, ['config_path' => $root . '/PUBLIC_HTML/config.json']),
];
foreach ($invalidCases as $name => $invalid) {
    file_put_contents($locatorPath, json_encode($invalid, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);
    exec($command . ' 2>&1', $ignored, $code);
    ok($code !== 0, "invalid locator accepted: {$name}");
}
file_put_contents($locatorPath, str_repeat('x', 65537)); chmod($locatorPath, 0600);
exec($command . ' 2>&1', $ignored, $code);
ok($code !== 0, 'oversize locator accepted');
file_put_contents($locatorPath, json_encode($locator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0640);
exec($command . ' 2>&1', $ignored, $code);
ok($code !== 0, 'loose locator mode accepted');
chmod($locatorPath, 0600); chmod($root . '/state', 0755);
exec($command . ' 2>&1', $ignored, $code);
ok($code !== 0, 'loose state directory mode accepted');
chmod($root . '/state', 0700);
file_put_contents($manifest, '{"schema_version":2}');
exec($command . ' 2>&1', $ignored, $code);
ok($code !== 0, 'manifest hash mismatch accepted');

foreach ([
    'unknown manifest key' => $manifestValue + ['unknown' => true],
    'unknown entrypoint key' => array_replace($manifestValue, ['entrypoint' => $manifestValue['entrypoint'] + ['unknown' => true]]),
    'wrong entrypoint size' => array_replace($manifestValue, ['entrypoint' => array_replace($manifestValue['entrypoint'], ['size' => filesize($target) + 1])]),
    'wrong entrypoint hash' => array_replace($manifestValue, ['entrypoint' => array_replace($manifestValue['entrypoint'], ['sha256' => str_repeat('0', 64)])]),
] as $name => $badManifest) {
    file_put_contents($manifest, json_encode($badManifest, JSON_THROW_ON_ERROR)); chmod($manifest, 0600);
    $changedLocator = array_replace($locator, ['manifest_sha256' => hash_file('sha256', $manifest)]);
    file_put_contents($locatorPath, json_encode($changedLocator, JSON_THROW_ON_ERROR)); chmod($locatorPath, 0600);
    exec($command . ' 2>&1', $ignored, $code);
    ok($code !== 0, "invalid manifest accepted: {$name}");
}

fwrite(STDOUT, "stable bootstrap tests passed\n");
