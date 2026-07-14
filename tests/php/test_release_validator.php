<?php

declare(strict_types=1);

function fail(string $message): never { fwrite(STDERR, "FAIL: {$message}\n"); exit(1); }
function ok(bool $condition, string $message): void { if (!$condition) fail($message); }
function rrmdir(string $path): void {
    if (!is_dir($path) || is_link($path)) { @unlink($path); return; }
    foreach (array_diff(scandir($path) ?: [], ['.', '..']) as $name) rrmdir($path . '/' . $name);
    @rmdir($path);
}

$exampleConfig = json_decode((string) file_get_contents(dirname(__DIR__, 2) . '/config/config.example.json'), true, 32, JSON_THROW_ON_ERROR);
ok(isset($exampleConfig['dedup_path']) && is_string($exampleConfig['dedup_path']), 'example config is missing dedup_path');
ok(str_starts_with($exampleConfig['dedup_path'], '/') && !str_contains(strtolower($exampleConfig['dedup_path']), '/public_html/'), 'example dedup_path must be absolute and private');

$class = dirname(__DIR__, 2) . '/src/ReleaseValidator.php';
ok(is_file($class), 'ReleaseValidator is missing');
require $class;

$serverHome = sys_get_temp_dir() . '/release-validator-' . bin2hex(random_bytes(8));
$root = $serverHome . '/release';
$config = $serverHome . '/config.json';
register_shutdown_function(static fn() => rrmdir($serverHome));
mkdir($root, 0700, true); chmod($serverHome, 0700); chmod($root, 0700);
file_put_contents($config, "{\"marker\":\"verified-config\"}\n"); chmod($config, 0600);
mkdir($root . '/bin', 0700); chmod($root . '/bin', 0700);
mkdir($root . '/vendor', 0700); chmod($root . '/vendor', 0700);
mkdir($root . '/src', 0700); chmod($root . '/src', 0700);
$runtimeDependencies = [
    'CanonicalEmail.php', 'SystemMailAuthenticator.php', 'SystemAlertFormatter.php', 'SendmailClient.php',
    'SendmailProcessAdapter.php', 'NativeSendmailProcessAdapter.php',
    'DeliveryHealthMonitor.php', 'PrivateStateFilesystem.php',
    'NativePrivateStateFilesystem.php',
];
foreach ($runtimeDependencies as $name) {
    copy(dirname(__DIR__, 2) . '/src/' . $name, $root . '/src/' . $name);
    chmod($root . '/src/' . $name, 0600);
}
file_put_contents($root . '/bin/mail-to-lineworks.php', <<<'PHP'
<?php
declare(strict_types=1);
require dirname(__DIR__).'/vendor/autoload.php';
$input = stream_get_contents(STDIN);
$magic = "XSERVER-MAIL-FRAME\0\x01";
$headerLength = strlen($magic) + 8;
$length = is_string($input) && strlen($input) >= $headerLength ? unpack('Nhigh/Nlow', substr($input, strlen($magic), 8)) : false;
$configLength = is_array($length) && $length['high'] === 0 ? $length['low'] : -1;
$config = $configLength >= 0 ? substr($input, $headerLength, $configLength) : false;
$raw = $configLength >= 0 ? substr($input, $headerLength + $configLength) : false;
exit(getenv('MAIL_NOTIFIER_STDIN_FRAME') === '1'
    && getenv('MAIL_NOTIFIER_CONFIG_FD') === false
    && str_starts_with((string) $input, $magic)
    && in_array('--check-message', $argv, true)
    && is_string($raw) && str_contains($raw, 'Message-ID: <validator@example.invalid>')
    && RaceFixture\Dependency::value() === 'verified'
    && $config === "{\"marker\":\"verified-config\"}\n" ? 0 : 9);
PHP);
chmod($root . '/bin/mail-to-lineworks.php', 0700);
file_put_contents($root . '/vendor/autoload.php', <<<'PHP'
<?php
spl_autoload_register(static function (string $class): void {
    if ($class === 'XserverMail\\NotifierConfig') eval('namespace XserverMail; final class NotifierConfig {}');
    if ($class === 'RaceFixture\\Dependency') require dirname(__DIR__) . '/src/Dependency.php';
});
return new stdClass();
PHP); chmod($root . '/vendor/autoload.php', 0600);
file_put_contents($root . '/src/NotifierConfig.php', "<?php\nnamespace XserverMail; final class NotifierConfig {}\n");
chmod($root . '/src/NotifierConfig.php', 0600);
file_put_contents($root . '/src/Dependency.php', "<?php\nnamespace RaceFixture; final class Dependency { public static function value(): string { return 'verified'; } }\n");
chmod($root . '/src/Dependency.php', 0600);

$entries = [];
$fixturePaths = ['bin' => 'directory', 'vendor' => 'directory', 'src' => 'directory', 'bin/mail-to-lineworks.php' => 'file', 'vendor/autoload.php' => 'file', 'src/Dependency.php' => 'file', 'src/NotifierConfig.php' => 'file'];
foreach ($runtimeDependencies as $name) $fixturePaths['src/' . $name] = 'file';
foreach ($fixturePaths as $path => $type) {
    $absolute = $root . '/' . $path;
    $entries[] = [
        'path' => $path, 'type' => $type,
        'mode' => $type === 'directory' || $path === 'bin/mail-to-lineworks.php' ? 0700 : 0600,
        'size' => $type === 'file' ? filesize($absolute) : 0,
        'sha256' => $type === 'file' ? hash_file('sha256', $absolute) : null,
    ];
}
$request = ['schema_version' => 1, 'release_path' => $root, 'entrypoint' => 'bin/mail-to-lineworks.php', 'config_path' => $config, 'manifest' => $entries];
$validator = new XserverMail\ReleaseValidator();
$result = $validator->validate($request);
foreach (['manifest', 'php_cli', 'autoload', 'absolute_cli_dry_run'] as $field) ok($result[$field] === 'PASS', "{$field} did not pass");
ok($result['symlinks'] === 0, 'symlink count did not pass');
$manifestByPath = [];
foreach ($request['manifest'] as $entry) $manifestByPath[$entry['path']] = $entry;
foreach ($runtimeDependencies as $name) {
    $path = 'src/' . $name;
    ok(isset($manifestByPath[$path]), $path . ' missing from manifest');
    ok($manifestByPath[$path]['mode'] === 0600, $path . ' must be mode 0600');
    ok($manifestByPath[$path]['sha256'] === hash_file('sha256', dirname(__DIR__, 2) . '/' . $path),
        $path . ' hash is not pinned to tracked source');
    $omitted = $request;
    $omitted['manifest'] = array_values(array_filter($omitted['manifest'],
        static fn(array $entry): bool => $entry['path'] !== $path));
    try { $validator->validate($omitted); fail($path . ' omission accepted'); } catch (RuntimeException) {}
    $tampered = $request;
    foreach ($tampered['manifest'] as &$entry) if ($entry['path'] === $path) $entry['sha256'] = str_repeat('0', 64);
    unset($entry);
    try { $validator->validate($tampered); fail($path . ' tamper accepted'); } catch (RuntimeException) {}
    $wrongMode = $request;
    foreach ($wrongMode['manifest'] as &$entry) if ($entry['path'] === $path) $entry['mode'] = 0640;
    unset($entry);
    try { $validator->validate($wrongMode); fail($path . ' wrong mode accepted'); } catch (RuntimeException) {}
}
$validatorSource = (string) file_get_contents($class);
ok(str_contains($validatorSource, "'--check-message'"), 'validator does not execute an RFC822 parser dry-run');
ok(str_contains($validatorSource, 'require $argv[1]'), 'validator does not execute Composer from its verified real path');
ok(str_contains($validatorSource, 'MAIL_NOTIFIER_STDIN_FRAME'), 'validator does not enable the stdin frame protocol');
ok(preg_match("/\['MAIL_NOTIFIER_CONFIG_FD'\]\s*=/", $validatorSource) !== 1,
    'validator still passes configuration through a numbered descriptor');
$auditSource = substr($validatorSource, strpos($validatorSource, 'public function auditPublicRoot'),
    strpos($validatorSource, 'private function manifestMap') - strpos($validatorSource, 'public function auditPublicRoot'));
foreach (['file_exists(', 'is_link('] as $followingProbe) {
    ok(!str_contains($auditSource, $followingProbe), 'public audit uses a second pathname probe instead of lstat');
}
ok(preg_match('/(?<!l)stat\s*\(/', $auditSource) !== 1, 'public audit uses target-following stat');
ok(!str_contains($auditSource, "!function_exists('posix_geteuid') ||"),
    'public audit accepts ownership when euid cannot be proven');
ok(str_contains($auditSource, "if (!function_exists('posix_geteuid'))"),
    'public audit does not fail closed when euid lookup is unavailable');

$badConfigRequest = $request;
$badConfigRequest['config_path'] = $serverHome . '/public_html/config.json';
try { $validator->validate($badConfigRequest); fail('public config path accepted'); } catch (RuntimeException) {}
chmod($config, 0640);
try { $validator->validate($request); fail('non-0600 config accepted'); } catch (RuntimeException) {}
chmod($config, 0600);
rename($config, $config . '.real'); symlink($config . '.real', $config);
try { $validator->validate($request); fail('symlink config accepted'); } catch (RuntimeException) {}
unlink($config); rename($config . '.real', $config);
$verifiedConfig = file_get_contents($config);
file_put_contents($config, str_repeat('x', 65537)); chmod($config, 0600);
try { $validator->validate($request); fail('oversized config accepted'); } catch (RuntimeException) {}
file_put_contents($config, $verifiedConfig); chmod($config, 0600);

$configRaceRan = false;
$configRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($config, &$configRaceRan): void {
        if ($phase !== 'config-open' || $path !== $config || $configRaceRan) return;
        $configRaceRan = true;
        rename($config, $config . '.verified');
        file_put_contents($config, "{\"marker\":\"replaced-config\"}\n"); chmod($config, 0600);
    },
);
try { $configRaceValidator->validate($request); fail('config lstat/open replacement accepted'); } catch (RuntimeException) {}
ok($configRaceRan, 'config open race was not injected');
unlink($config); rename($config . '.verified', $config);

$inPlaceRaceRan = false;
$inPlaceRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase) use ($config, &$inPlaceRaceRan): void {
        if ($phase !== 'entry' || $inPlaceRaceRan) return;
        $inPlaceRaceRan = true;
        file_put_contents($config, "{\"marker\":\"replaced-config\"}\n"); chmod($config, 0600);
    },
);
$inPlaceResult = $inPlaceRaceValidator->validate($request);
ok($inPlaceRaceRan, 'same-size in-place config race was not injected');
ok($inPlaceResult['absolute_cli_dry_run'] === 'PASS', 'child did not receive verified config snapshot bytes');
file_put_contents($config, $verifiedConfig); chmod($config, 0600);

foreach (['fail', 'bad-mode', 'short-write'] as $snapshotFault) {
    $faultValidator = new XserverMail\ReleaseValidator(
        static fn(string $phase) => $phase === 'config-snapshot' ? $snapshotFault : null,
    );
    try { $faultValidator->validate($request); fail("snapshot {$snapshotFault} accepted"); } catch (RuntimeException) {}
}

$raceRan = false;
$raceValidator = new XserverMail\ReleaseValidator(static function (string $phase) use ($root, &$raceRan): void {
    if ($phase !== 'entry') return;
    $raceRan = true;
    $entry = $root . '/bin/mail-to-lineworks.php';
    $dependency = $root . '/src/Dependency.php';
    rename($entry, $entry . '.verified');
    file_put_contents($entry, "<?php\nexit(7);\n"); chmod($entry, 0700);
    rename($dependency, $dependency . '.verified');
    file_put_contents($dependency, "<?php\nnamespace RaceFixture; final class Dependency { public static function value(): string { return 'replaced'; } }\n");
    chmod($dependency, 0600);
});
try { $raceValidator->validate($request); fail('pre-spawn path replacement race accepted'); } catch (RuntimeException) {}
ok($raceRan, 'validator did not exercise the pre-spawn path replacement race');
unlink($root . '/bin/mail-to-lineworks.php');
rename($root . '/bin/mail-to-lineworks.php.verified', $root . '/bin/mail-to-lineworks.php');
unlink($root . '/src/Dependency.php');
rename($root . '/src/Dependency.php.verified', $root . '/src/Dependency.php');

$bad = $request;
$bad['manifest'][2]['sha256'] = str_repeat('0', 64);
try { $validator->validate($bad); fail('hash mismatch accepted'); } catch (RuntimeException) {}
$bad = $request;
$bad['manifest'][2]['mode'] = 0600;
try { $validator->validate($bad); fail('mode mismatch accepted'); } catch (RuntimeException) {}
symlink('/dev/null', $root . '/unexpected-link');
try { $validator->validate($request); fail('symlink accepted'); } catch (RuntimeException) {}
unlink($root . '/unexpected-link');
file_put_contents($root . '/extra', 'x'); chmod($root . '/extra', 0600);
try { $validator->validate($request); fail('extra file accepted'); } catch (RuntimeException) {}
unlink($root . '/extra');

$verifiedAutoload = file_get_contents($root . '/vendor/autoload.php');
$pathAutoload = "<?php\nthrow new RuntimeException('path autoload must not execute');\n";
file_put_contents($root . '/vendor/autoload.php', $pathAutoload);
foreach ($request['manifest'] as &$entry) if ($entry['path'] === 'vendor/autoload.php') {
    $entry['size'] = strlen($pathAutoload); $entry['sha256'] = hash('sha256', $pathAutoload);
}
unset($entry);
try { $validator->validate($request); fail('invalid Composer autoload accepted'); } catch (RuntimeException) {}
file_put_contents($root . '/vendor/autoload.php', $verifiedAutoload); chmod($root . '/vendor/autoload.php', 0600);
foreach ($request['manifest'] as &$entry) if ($entry['path'] === 'vendor/autoload.php') {
    $entry['size'] = strlen($verifiedAutoload); $entry['sha256'] = hash('sha256', $verifiedAutoload);
}
unset($entry);

$named = $serverHome . '/my-public_html-backup';
rename($root, $named);
$namedRequest = $request; $namedRequest['release_path'] = $named;
$namedResult = $validator->validate($namedRequest);
ok($namedResult['manifest'] === 'PASS', 'non-segment public_html substring was rejected');
rename($named, $root);

$domainOne = $serverHome . '/first.example.invalid';
$domainTwo = $serverHome . '/second.example.invalid';
mkdir($domainOne, 0700); chmod($domainOne, 0700);
mkdir($domainTwo, 0700); chmod($domainTwo, 0700);
mkdir($domainOne . '/public_html', 0755); chmod($domainOne . '/public_html', 0755);
mkdir($domainTwo . '/public_html', 0700); chmod($domainTwo . '/public_html', 0700);
file_put_contents($domainOne . '/public_html/unrelated.txt', 'must-not-be-read');
chmod($domainOne . '/public_html/unrelated.txt', 0000);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit === [
    'schema_version' => 3,
    'home_symlink' => false,
    'home_mode' => 0700,
    'public_roots_scanned' => 2,
    'symlinks' => 0,
    'known_product_matches' => 0,
    'untrusted_subtrees' => 0,
    'untrusted_entries' => 0,
], 'multiple domain public roots were not audited with the exact metadata-only schema');

$writableBackup = $domainOne . '/public_html/wordpress-backup';
mkdir($writableBackup, 0775); chmod($writableBackup, 0775);
$writableSentinel = $writableBackup . '/must-not-be-opened';
file_put_contents($writableSentinel, 'sentinel'); chmod($writableSentinel, 0000);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit['known_product_matches'] === 0 && $audit['untrusted_subtrees'] === 1
    && $audit['untrusted_entries'] === 0,
    'writable unrelated subtree was not skipped with an explicit bounded count');

$knownWritable = $domainOne . '/public_html/mail-forward-command.php';
mkdir($knownWritable, 0775); chmod($knownWritable, 0775);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit['known_product_matches'] === 1 && $audit['untrusted_subtrees'] === 2,
    'known basename was not counted before skipping an untrusted subtree');
rrmdir($knownWritable);

$untrustedOpened = false;
$skipValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($writableBackup, &$untrustedOpened): void {
        if ($phase === 'audit-open' && $path === $writableBackup) $untrustedOpened = true;
    },
);
$skipValidator->auditPublicRoot($serverHome, []);
ok(!$untrustedOpened, 'untrusted subtree was opened or descended');

$untrustedRaceRan = false;
$untrustedRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($writableBackup, &$untrustedRaceRan): void {
        if ($phase !== 'audit-untrusted' || $path !== $writableBackup || $untrustedRaceRan) return;
        $untrustedRaceRan = true;
        rename($writableBackup, $writableBackup . '-verified');
        mkdir($writableBackup, 0775); chmod($writableBackup, 0775);
    },
);
try { $untrustedRaceValidator->auditPublicRoot($serverHome, []); fail('untrusted subtree exchange accepted'); } catch (RuntimeException) {}
ok($untrustedRaceRan, 'untrusted subtree race was not injected');
rrmdir($writableBackup); rename($writableBackup . '-verified', $writableBackup);
chmod($writableSentinel, 0600); rrmdir($writableBackup);

$outsideTarget = $serverHome . '/outside-target';
mkdir($outsideTarget, 0700); chmod($outsideTarget, 0700);
file_put_contents($outsideTarget . '/must-not-be-read', 'private target content');
chmod($outsideTarget . '/must-not-be-read', 0000);
$unrelatedLink = $domainOne . '/public_html/unrelated-link';
symlink($outsideTarget, $unrelatedLink);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit['symlinks'] === 1, 'unrelated public symlink was not counted without following it');
ok($audit['known_product_matches'] === 0, 'unrelated symlink was counted as known product content');
unlink($unrelatedLink);

$knownLink = $domainOne . '/public_html/mail-forward-command.php';
symlink($outsideTarget, $knownLink);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit['symlinks'] === 1 && $audit['known_product_matches'] === 1,
    'known product basename symlink was not counted for final rejection');
unlink($knownLink);

$special = $domainOne . '/public_html/unrelated-fifo';
if (function_exists('posix_mkfifo')) {
    posix_mkfifo($special, 0600);
    $audit = $validator->auditPublicRoot($serverHome, []);
    ok($audit['untrusted_entries'] === 1, 'special public entry was not skipped explicitly');
    unlink($special);
}

$symlinkRaceRan = false;
symlink($outsideTarget, $unrelatedLink);
$symlinkRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($unrelatedLink, &$symlinkRaceRan): void {
        if ($phase !== 'audit-symlink' || $path !== $unrelatedLink || $symlinkRaceRan) return;
        $symlinkRaceRan = true;
        unlink($unrelatedLink);
        mkdir($unrelatedLink, 0700); chmod($unrelatedLink, 0700);
    },
);
try { $symlinkRaceValidator->auditPublicRoot($serverHome, []); fail('symlink-to-directory exchange accepted'); } catch (RuntimeException) {}
ok($symlinkRaceRan, 'symlink exchange race was not injected');
rrmdir($unrelatedLink);

$fileRaceRan = false;
$ordinaryFile = $domainOne . '/public_html/ordinary-race-file';
file_put_contents($ordinaryFile, 'metadata only'); chmod($ordinaryFile, 0600);
$fileRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($ordinaryFile, $outsideTarget, &$fileRaceRan): void {
        if ($phase !== 'audit-file' || $path !== $ordinaryFile || $fileRaceRan) return;
        $fileRaceRan = true;
        unlink($ordinaryFile);
        symlink($outsideTarget, $ordinaryFile);
    },
);
try { $fileRaceValidator->auditPublicRoot($serverHome, []); fail('file-to-symlink exchange accepted'); } catch (RuntimeException) {}
ok($fileRaceRan, 'file exchange race was not injected');
unlink($ordinaryFile);

$targetRaceRan = false;
symlink($outsideTarget, $unrelatedLink);
$targetRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($unrelatedLink, $serverHome, &$targetRaceRan): void {
        if ($phase !== 'audit-symlink' || $path !== $unrelatedLink || $targetRaceRan) return;
        $targetRaceRan = true;
        unlink($unrelatedLink);
        symlink($serverHome . '/different-outside-target', $unrelatedLink);
    },
);
try { $targetRaceValidator->auditPublicRoot($serverHome, []); fail('symlink target replacement accepted'); } catch (RuntimeException) {}
ok($targetRaceRan, 'symlink target race was not injected');
unlink($unrelatedLink);
chmod($outsideTarget . '/must-not-be-read', 0600);
rrmdir($outsideTarget);

chmod($domainOne . '/public_html/unrelated.txt', 0600);
mkdir($domainTwo . '/public_html/xserver-mail-lineworks', 0700);
chmod($domainTwo . '/public_html/xserver-mail-lineworks', 0700);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit['known_product_matches'] === 1, 'known product basename was not counted across domain roots');

$rootPublic = $serverHome . '/public_html';
mkdir($rootPublic, 0755); chmod($rootPublic, 0755);
file_put_contents($rootPublic . '/mail-forward-command.php', 'metadata only'); chmod($rootPublic . '/mail-forward-command.php', 0600);
$audit = $validator->auditPublicRoot($serverHome, ['xserver-mail-lineworks', 'mail-forward-command.php']);
ok($audit['public_roots_scanned'] === 3, 'optional root-level public_html was not audited');
ok($audit['known_product_matches'] === 2, 'known product basename was not counted in root public_html');

symlink($domainOne, $serverHome . '/linked-domain');
try { $validator->auditPublicRoot($serverHome, []); fail('symlinked domain entry accepted'); } catch (RuntimeException) {}
unlink($serverHome . '/linked-domain');
$unrelatedWritable = $serverHome . '/unrelated-runtime-data';
mkdir($unrelatedWritable, 0775); chmod($unrelatedWritable, 0775);
$audit = $validator->auditPublicRoot($serverHome, []);
ok($audit['public_roots_scanned'] === 3, 'unrelated top-level directory blocked public-root audit');
rmdir($unrelatedWritable);
rename($domainOne . '/public_html', $domainOne . '/public_html-real');
symlink($domainOne . '/public_html-real', $domainOne . '/public_html');
try { $validator->auditPublicRoot($serverHome, []); fail('symlinked domain public root accepted'); } catch (RuntimeException) {}
unlink($domainOne . '/public_html');
rename($domainOne . '/public_html-real', $domainOne . '/public_html');

chmod($domainOne, 0770);
try { $validator->auditPublicRoot($serverHome, []); fail('writable domain directory accepted'); } catch (RuntimeException) {}
chmod($domainOne, 0700);
chmod($domainOne . '/public_html', 0775);
try { $validator->auditPublicRoot($serverHome, []); fail('writable public root accepted'); } catch (RuntimeException) {}
chmod($domainOne . '/public_html', 0755);
chmod($serverHome, 0770);
try { $validator->auditPublicRoot($serverHome, []); fail('writable server home accepted'); } catch (RuntimeException) {}
chmod($serverHome, 0700);
chmod($rootPublic . '/mail-forward-command.php', 0660);
$audit = $validator->auditPublicRoot($serverHome, []);
ok($audit['untrusted_entries'] === 1, 'writable public content was not skipped explicitly');
chmod($rootPublic . '/mail-forward-command.php', 0600);

$laterRootActivityRan = false;
$laterRootActivityValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($domainOne, $domainTwo, &$laterRootActivityRan): void {
        if ($phase !== 'audit-open' || $path !== $domainOne . '/public_html' || $laterRootActivityRan) return;
        $laterRootActivityRan = true;
        sleep(1);
        file_put_contents($domainTwo . '/public_html/normal-web-update', 'normal activity');
        chmod($domainTwo . '/public_html/normal-web-update', 0600);
    },
);
$audit = $laterRootActivityValidator->auditPublicRoot($serverHome, ['normal-web-update']);
ok($laterRootActivityRan, 'later-root activity was not injected');
ok($audit['public_roots_scanned'] === 3, 'normal activity after an audited root caused a false race failure');
ok($audit['known_product_matches'] === 1, 'later-root activity was not audited from its current snapshot');
unlink($domainTwo . '/public_html/normal-web-update');

$openRaceRan = false;
$openRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($domainOne, &$openRaceRan): void {
        $public = $domainOne . '/public_html';
        if ($phase !== 'audit-open' || $path !== $public || $openRaceRan) return;
        $openRaceRan = true;
        rename($public, $public . '-verified');
        mkdir($public, 0755); chmod($public, 0755);
    },
);
try { $openRaceValidator->auditPublicRoot($serverHome, []); fail('public root replacement before iterator open accepted'); } catch (RuntimeException) {}
ok($openRaceRan, 'public root open race was not injected');
rrmdir($domainOne . '/public_html');
rename($domainOne . '/public_html-verified', $domainOne . '/public_html');

$domainRaceRan = false;
$domainRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($domainOne, &$domainRaceRan): void {
        if ($phase !== 'audit-domain' || $path !== $domainOne || $domainRaceRan) return;
        $domainRaceRan = true;
        rename($domainOne, $domainOne . '-verified');
        mkdir($domainOne, 0700); chmod($domainOne, 0700);
    },
);
try { $domainRaceValidator->auditPublicRoot($serverHome, []); fail('domain replacement before public root discovery accepted'); } catch (RuntimeException) {}
ok($domainRaceRan, 'domain discovery race was not injected');
rrmdir($domainOne);
rename($domainOne . '-verified', $domainOne);

$candidateRaceRan = false;
$candidateRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($domainOne, &$candidateRaceRan): void {
        if ($phase !== 'audit-candidate' || $path !== $domainOne || $candidateRaceRan) return;
        $candidateRaceRan = true;
        rename($domainOne, $domainOne . '-verified');
        mkdir($domainOne, 0700); chmod($domainOne, 0700);
        mkdir($domainOne . '/public_html', 0755); chmod($domainOne . '/public_html', 0755);
    },
);
try { $candidateRaceValidator->auditPublicRoot($serverHome, []); fail('domain replacement after candidate discovery accepted'); } catch (RuntimeException) {}
ok($candidateRaceRan, 'candidate discovery race was not injected');
rrmdir($domainOne);
rename($domainOne . '-verified', $domainOne);

$nested = $domainOne . '/public_html/nested';
mkdir($nested, 0755); chmod($nested, 0755);
$descentRaceRan = false;
$descentRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($nested, &$descentRaceRan): void {
        if ($phase !== 'audit-descend' || $path !== $nested || $descentRaceRan) return;
        $descentRaceRan = true;
        rename($nested, $nested . '-verified');
        mkdir($nested, 0755); chmod($nested, 0755);
    },
);
try { $descentRaceValidator->auditPublicRoot($serverHome, []); fail('nested directory replacement before descent accepted'); } catch (RuntimeException) {}
ok($descentRaceRan, 'nested descent race was not injected');
rrmdir($nested);
rename($nested . '-verified', $nested);

$descentSymlinkRaceRan = false;
$descentSymlinkRaceValidator = new XserverMail\ReleaseValidator(
    static function (string $phase, ?string $path = null) use ($nested, &$descentSymlinkRaceRan): void {
        if ($phase !== 'audit-descend' || $path !== $nested || $descentSymlinkRaceRan) return;
        $descentSymlinkRaceRan = true;
        rename($nested, $nested . '-verified');
        symlink($nested . '-verified', $nested);
    },
);
try { $descentSymlinkRaceValidator->auditPublicRoot($serverHome, []); fail('directory-to-symlink exchange accepted'); } catch (RuntimeException) {}
ok($descentSymlinkRaceRan, 'directory-to-symlink race was not injected');
unlink($nested);
rename($nested . '-verified', $nested);

fwrite(STDOUT, "release validator tests passed\n");
