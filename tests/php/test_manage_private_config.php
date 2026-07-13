<?php
declare(strict_types=1);

$source = dirname(__DIR__, 2) . '/bin/manage-private-config.php';
if (!is_file($source)) {
    fwrite(STDERR, "FAIL: bin/manage-private-config.php does not exist\n");
    exit(1);
}

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

/** @return array{code: int, stdout: string, stderr: string} */
function runHelper(string $helper, array|string $request, ?array $environment = null): array
{
    $pipes = [];
    $process = proc_open([PHP_BINARY, $helper], [
        0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'],
    ], $pipes, null, $environment);
    check(is_resource($process), 'helper process must start');
    $input = is_string($request) ? $request : json_encode($request, JSON_THROW_ON_ERROR);
    fwrite($pipes[0], $input);
    fclose($pipes[0]);
    $stdout = stream_get_contents($pipes[1]);
    fclose($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[2]);
    return ['code' => proc_close($process), 'stdout' => $stdout, 'stderr' => $stderr];
}

function checkFixedHealthFailure(array $result, string $message): void
{
    check($result['code'] !== 0 && $result['stdout'] === ''
        && $result['stderr'] === "private config operation failed\n", $message);
}

/** @return array{process: resource, pipes: array<int, resource>} */
function startHelper(string $helper, array $request): array
{
    $pipes = [];
    $process = proc_open([PHP_BINARY, $helper], [
        0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'], 3 => ['pipe', 'w'],
    ], $pipes, null, ['PRIVATE_CONFIG_TEST_READY' => '1']);
    check(is_resource($process), 'concurrent helper process must start');
    fwrite($pipes[0], json_encode($request, JSON_THROW_ON_ERROR));
    fclose($pipes[0]);
    return ['process' => $process, 'pipes' => $pipes];
}

/** @return array{process: resource, pipes: array<int, resource>} */
function startHealthHelper(string $helper): array
{
    $pipes = [];
    $process = proc_open([PHP_BINARY, $helper], [
        0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'],
        3 => ['pipe', 'w'], 4 => ['pipe', 'r'],
    ], $pipes, null, ['PRIVATE_CONFIG_TEST_HEALTH_READY' => '1']);
    check(is_resource($process), 'health helper process must start');
    fwrite($pipes[0], json_encode(
        ['schema_version' => 1, 'operation' => 'health-summary'], JSON_THROW_ON_ERROR
    ));
    fclose($pipes[0]);
    return ['process' => $process, 'pipes' => $pipes];
}

/** @return array{process: resource, pipes: array<int, resource>} */
function startMissingHealthHelper(string $helper): array
{
    $pipes = [];
    $process = proc_open([PHP_BINARY, $helper], [
        0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'],
        3 => ['pipe', 'w'], 4 => ['pipe', 'r'],
    ], $pipes, null, ['PRIVATE_CONFIG_TEST_MISSING_READY' => '1']);
    check(is_resource($process), 'missing-health helper process must start');
    fwrite($pipes[0], json_encode(
        ['schema_version' => 1, 'operation' => 'health-summary'], JSON_THROW_ON_ERROR
    ));
    fclose($pipes[0]);
    return ['process' => $process, 'pipes' => $pipes];
}

/** @return array{process: resource, pipes: array<int, resource>} */
function startJournalHelper(string $helper, array $request, string $hook, string $hookValue = '1'): array
{
    $pipes = [];
    $process = proc_open([PHP_BINARY, $helper], [
        0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'],
        3 => ['pipe', 'w'], 4 => ['pipe', 'r'],
    ], $pipes, null, [$hook => $hookValue]);
    check(is_resource($process), 'journal helper process must start');
    fwrite($pipes[0], json_encode($request, JSON_THROW_ON_ERROR));
    fclose($pipes[0]);
    return ['process' => $process, 'pipes' => $pipes];
}

function waitHelperReady(array &$running): void
{
    $ready = fread($running['pipes'][3], 1);
    fclose($running['pipes'][3]);
    unset($running['pipes'][3]);
    check($ready === 'R', 'concurrent helper must reach the lock-ready barrier');
}

/** @return array{code: int, stdout: string, stderr: string} */
function finishHelper(array $running): array
{
    $stdout = stream_get_contents($running['pipes'][1]);
    fclose($running['pipes'][1]);
    $stderr = stream_get_contents($running['pipes'][2]);
    fclose($running['pipes'][2]);
    return ['code' => proc_close($running['process']), 'stdout' => $stdout, 'stderr' => $stderr];
}

function writeConfig(string $path, array $config, int $mode = 0600): string
{
    $bytes = json_encode($config, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
    file_put_contents($path, $bytes);
    chmod($path, $mode);
    return $bytes;
}

/** @return list<string> */
function helperBoundaryAddresses(int $count, ?int $toBytes = null): array
{
    $localLengths = array_fill(0, $count, 2);
    if ($toBytes !== null) {
        $localTotal = $toBytes - ($count * 16) - max(0, $count - 1);
        $localLengths = array_fill(0, $count - 1, 10);
        $localLengths[] = $localTotal - array_sum($localLengths);
    }
    $addresses = [];
    foreach ($localLengths as $index => $length) {
        $prefix = chr(97 + intdiv($index, 26)) . chr(97 + ($index % 26));
        $addresses[] = $prefix . str_repeat('x', $length - 2) . '@example.invalid';
    }
    return $addresses;
}

$temporary = sys_get_temp_dir() . '/private-config-test-' . bin2hex(random_bytes(8));
$home = $temporary . '/home/example';
$configDirectory = $home . '/mail-lineworks/private';
mkdir($configDirectory, 0700, true);
$helper = $temporary . '/manage-private-config.php';
$body = file_get_contents($source);
check(is_string($body), 'helper source must be readable');
check(str_contains($body, 'function scopeJournalPersistentSlots'),
    'journal recovery must use fixed persistent transaction slots');
check(str_contains($body, 'function selectScopeJournalGeneration'),
    'journal recovery must select one checksum-valid generation deterministically');
check(str_contains($body, 'function repairScopeJournalSlot'),
    'one interrupted inactive-slot rewrite must be repaired through its retained descriptor');
check(str_contains($body, 'function initializeScopeJournalBootstrapRemnant'),
    'journal slots must isolate the narrow pre-generation bootstrap repair');
check(str_contains($body, 'const REQUEST_LIMIT = 262144;'),
    'two exact 64 KiB journal bodies must fit the bounded CAS request');
check(str_contains($body, 'no durable journal generation'),
    'bootstrap repair must document its one-way no-generation authorization boundary');
check(str_contains($body, 'exact strict-prefix bootstrap remnant'),
    'bootstrap repair must accept only zero or an exact canonical prefix');
check(!str_contains($body, 'function cleanupScopeJournalTransaction'),
    'persistent journal recovery must have no pathname cleanup phase');
$journalSourceStart = strpos($body, 'function scopeJournalPersistentSlots');
$journalSourceEnd = strpos($body, 'function validateConfigSchema');
check(is_int($journalSourceStart) && is_int($journalSourceEnd)
    && $journalSourceEnd > $journalSourceStart,
    'persistent journal source boundaries must be exact');
$journalPersistentSource = substr($body, $journalSourceStart,
    $journalSourceEnd - $journalSourceStart);
check(!str_contains($journalPersistentSource, 'unlink(')
    && !str_contains($journalPersistentSource, 'rename('),
    'persistent journal protocol must never unlink or rename a recovery artifact');
check(str_contains($body, '$home = dirname(__DIR__, 3);'), 'fixed dirname derivation must remain');
check(str_contains($body, "'/mail-lineworks/private/config.json'"), 'fixed config suffix must remain');
check(str_contains($body, "['uid'] !== posix_geteuid()"),
    'directory owner validation must remain in production source');
$body = str_replace('$home = dirname(__DIR__, 3);', '$home = ' . var_export($home, true) . ';', $body);
$lockReady = <<<'PHP'
    if (getenv('PRIVATE_CONFIG_TEST_READY') === '1') {
        $testReady = fopen('php://fd/3', 'wb');
        fwrite($testReady, 'R');
        fclose($testReady);
    }
PHP;
$body = str_replace(
    '    if (!flock($handle, LOCK_EX)) {',
    $lockReady . "\n" . '    if (!flock($handle, LOCK_EX)) {',
    $body,
    $readyCount,
);
check($readyCount === 1, 'test-only lock-ready insertion must be exact');
$body = str_replace(
    '    if (!flock($handle, LOCK_EX)) {',
    "    if (getenv('PRIVATE_CONFIG_TEST_BYPASS_OUTER_LOCK') !== '1'"
        . ' && !flock($handle, LOCK_EX)) {',
    $body, $outerLockBypassCount,
);
check($outerLockBypassCount === 1, 'test-only outer lock bypass insertion must be exact');
$healthBarrier = <<<'PHP'
    if (getenv('PRIVATE_CONFIG_TEST_HEALTH_READY') === '1') {
        $testReady = fopen('php://fd/3', 'wb');
        fwrite($testReady, 'R');
        fclose($testReady);
        $testContinue = fopen('php://fd/4', 'rb');
        fread($testContinue, 1);
        fclose($testContinue);
    }
PHP;
$body = preg_replace_callback(
    '~(function healthSummary\b.*?)(    assertDirectoryChain\(\$parentTrust\);\n'
        . '    clearstatcache\(true, \$path\);\n    \$after = @lstat\(\$path\);)~s',
    static fn (array $match): string => $match[1] . $healthBarrier . "\n" . $match[2],
    $body,
    1,
    $healthReadyCount,
);
check($healthReadyCount === 1, 'test-only health-ready insertion must be exact');
$missingHealthBarrier = <<<'PHP'
    if (getenv('PRIVATE_CONFIG_TEST_MISSING_READY') === '1') {
        $testReady = fopen('php://fd/3', 'wb');
        fwrite($testReady, 'R');
        fclose($testReady);
        $testContinue = fopen('php://fd/4', 'rb');
        fread($testContinue, 1);
        fclose($testContinue);
    }
PHP;
$body = preg_replace_callback(
    '~(function healthSummary\b.*?)(    \$before = @lstat\(\$path\);)~s',
    static fn (array $match): string => $match[1] . $missingHealthBarrier . "\n" . $match[2],
    $body,
    1,
    $missingHealthReadyCount,
);
check($missingHealthReadyCount === 1, 'test-only missing-health insertion must be exact');
$body = preg_replace_callback(
    '~(function healthSummary\b.*?)(\$before\[\x27uid\x27\] !== posix_geteuid\(\))~s',
    static fn (array $match): string => $match[1] . '(' . $match[2]
        . " || getenv('PRIVATE_CONFIG_TEST_WRONG_OWNER') === '1')",
    $body,
    1,
    $wrongOwnerHookCount,
);
check($wrongOwnerHookCount === 1, 'test-only wrong-owner insertion must be exact');
$journalBarrier = <<<'PHP'
    if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE') === 'target-opened') {
        $testReady = fopen('php://fd/3', 'wb');
        fwrite($testReady, 'R');
        fclose($testReady);
        $testContinue = fopen('php://fd/4', 'rb');
        fread($testContinue, 1);
        fclose($testContinue);
    }
PHP;
$body = str_replace(
    '        // Interposition boundary: retained-target-opened.',
    '        // Interposition boundary: retained-target-opened.' . "\n" . $journalBarrier,
    $body, $journalReadyCount,
);
check($journalReadyCount === 1, 'test-only retained-target barrier insertion must be exact');
$journalLockBarrier = <<<'PHP'
        if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE') === 'lock-held') {
            $testReady = fopen('php://fd/3', 'wb'); fwrite($testReady, 'R'); fclose($testReady);
            $testContinue = fopen('php://fd/4', 'rb'); fread($testContinue, 1); fclose($testContinue);
        }
PHP;
$body = str_replace(
    '        // Interposition boundary: retained-journal-lock-held.',
    '        // Interposition boundary: retained-journal-lock-held.' . "\n" . $journalLockBarrier,
    $body, $journalLockReadyCount,
);
check($journalLockReadyCount === 1, 'test-only retained journal lock barrier must be exact');
$body = str_replace(
    "if (!flock(\$lock['handle'], LOCK_EX))",
    "if (!flock(\$lock['handle'], getenv('PRIVATE_CONFIG_TEST_JOURNAL_NONBLOCK') === '1'"
        . " ? (LOCK_EX | LOCK_NB) : LOCK_EX))",
    $body, $journalNonblockCount,
);
check($journalNonblockCount === 1, 'test-only replacement-lock nonblocking hook must be exact');
$journalMissingTargetBarrier = <<<'PHP'
        if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE') === 'missing-target-created') {
            $testReady = fopen('php://fd/3', 'wb'); fwrite($testReady, 'R'); fclose($testReady);
            $testContinue = fopen('php://fd/4', 'rb'); fread($testContinue, 1); fclose($testContinue);
        }
        if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_CRASH') === 'missing-target-created') {
            throw new RuntimeException();
        }
PHP;
$body = str_replace(
    '        // Interposition boundary: retained-missing-target-created.',
    '        // Interposition boundary: retained-missing-target-created.' . "\n"
        . $journalMissingTargetBarrier,
    $body, $journalMissingTargetReadyCount,
);
check($journalMissingTargetReadyCount === 1,
    'test-only retained missing-target barrier must be exact');
$body = preg_replace_callback(
    '~(function persistentJournalOpenTarget\b.*?)(\$before\[\x27uid\x27\] !== posix_geteuid\(\))~s',
    static fn (array $match): string => $match[1] . '(' . $match[2]
        . " || getenv('PRIVATE_CONFIG_TEST_JOURNAL_WRONG_OWNER') === '1')",
    $body,
    1,
    $journalWrongOwnerCount,
);
check($journalWrongOwnerCount === 1, 'test-only journal wrong-owner hook must be exact');
$body = preg_replace_callback(
    '~(function scopeJournalLocation\b.*?)(    \$trust\[\$cursor\] = )~s',
    static fn (array $match): string => $match[1]
        . "    if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_PARENT_WRONG_OWNER') === '1') {\n"
        . "        throw new RuntimeException();\n    }\n" . $match[2],
    $body,
    1,
    $journalParentOwnerCount,
);
check($journalParentOwnerCount === 1, 'test-only journal parent-owner hook must be exact');
$partialRewriteHook = <<<'PHP'
    static $journalRewriteCall = 0;
    $journalRewriteCall++;
    if ((int) getenv('PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_CALL') === $journalRewriteCall) {
        $prefixLength = max(0, min(strlen($bytes) - 1,
            (int) getenv('PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_BYTES')));
        if ($prefixLength > 0) fwrite($handle, substr($bytes, 0, $prefixLength));
        fflush($handle);
        fsyncScopeJournalFile($handle);
        throw new RuntimeException();
    }
PHP;
$body = preg_replace_callback(
    '~(function rewriteScopeJournalDescriptor\b.*?)(    \$offset = 0;)~s',
    static fn (array $match): string => $match[1] . $partialRewriteHook . "\n" . $match[2],
    $body, 1, $journalPartialHookCount,
);
check($journalPartialHookCount === 1, 'test-only partial descriptor rewrite hook must be exact');
$body = str_replace(
    '    if (!fsync($handle)) {',
    "    if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_FILE_FSYNC_FAIL') === '1'"
        . ' || !fsync($handle)) {',
    $body,
    $journalFileFsyncHookCount,
);
check($journalFileFsyncHookCount === 1, 'journal writes must fsync retained descriptors');
$body = str_replace(
    '    if (!fsync($directoryHandle)) {',
    "    if (getenv('PRIVATE_CONFIG_TEST_JOURNAL_DIRECTORY_FSYNC_FAIL') === '1'"
        . ' || !fsync($directoryHandle)) {',
    $body,
    $journalDirectoryFsyncHookCount,
);
check($journalDirectoryFsyncHookCount === 1, 'journal writes must fsync the verified parent descriptor');
$body = preg_replace(
    "~if \(preg_match\('/\\\\A\\\\/home.*?throw new RuntimeException\(\);\n    }~s",
    "if (false) { throw new RuntimeException();\n    }",
    $body,
    1,
    $count,
);
check(is_string($body) && $count === 1, 'test-only home validation substitution must be exact');
file_put_contents($helper, $body);
chmod($helper, 0700);
$configPath = $configDirectory . '/config.json';
$old = ['webhook_url' => 'https://webhook.worksmobile.com/message/test-placeholder',
    'error_recipients' => ['operator@example.invalid'],
    'notification_pinned_targets' => [], 'notification_targets' => [],
    'system_mail_hmac_key' => rtrim(strtr(base64_encode(str_repeat('k', 32)), '+/', '-_'), '='),
    'log_path' => $configDirectory . '/notifier.jsonl', 'unknown' => 'keep'];
$oldBytes = writeConfig($configPath, $old);

try {
    $read = runHelper($helper, ['schema_version' => 1, 'operation' => 'read']);
    check($read['code'] === 0, 'read must succeed');
    $decoded = json_decode($read['stdout'], true, 16, JSON_THROW_ON_ERROR);
    check($decoded['config'] === $old, 'read must return config');
    check($decoded['sha256'] === hash('sha256', $oldBytes), 'read hash must use exact bytes');
    $missingHealth = runHelper($helper, ['schema_version' => 1, 'operation' => 'health-summary']);
    check($missingHealth['code'] === 0, 'missing health summary must succeed');
    check(json_decode($missingHealth['stdout'], true, 16, JSON_THROW_ON_ERROR) === [
        'schema_version' => 1, 'state' => 'missing', 'changed_at' => null,
        'classification' => null, 'next_observation_sequence' => 0,
        'last_applied_sequence' => 0,
    ], 'missing health summary must use the fixed redacted schema');
    $missingRaceParent = $home . '/missing-health-race';
    $missingRaceReplacement = $home . '/missing-health-replacement';
    mkdir($missingRaceParent . '/child', 0700, true);
    mkdir($missingRaceReplacement, 0700);
    $missingRaceConfig = $old;
    $missingRaceConfig['log_path'] = $missingRaceParent . '/child/notifier.jsonl';
    writeConfig($configPath, $missingRaceConfig);
    $runningMissingHealth = startMissingHealthHelper($helper);
    check(fread($runningMissingHealth['pipes'][3], 1) === 'R',
        'missing-health helper must reach pre-lstat race barrier');
    fclose($runningMissingHealth['pipes'][3]);
    rename($missingRaceParent, $missingRaceParent . '-moved');
    symlink($missingRaceReplacement, $missingRaceParent);
    fwrite($runningMissingHealth['pipes'][4], 'C');
    fclose($runningMissingHealth['pipes'][4]);
    $missingRaceResult = finishHelper($runningMissingHealth);
    check($missingRaceResult['code'] !== 0
        && $missingRaceResult['stdout'] === ''
        && $missingRaceResult['stderr'] === "private config operation failed\n",
        'missing health summary must fail redacted after ancestor replacement');
    unlink($missingRaceParent);
    rename($missingRaceParent . '-moved', $missingRaceParent);
    writeConfig($configPath, $old);

    $journalDirectory = $home . '/private/xserver-mail-lineworks/deploy-transactions';
    mkdir($journalDirectory, 0700, true);
    $journalBody = "{\"schema_version\":2}\n";
    $journalFiles = static function (string $kind) use ($journalDirectory): array {
        $prefix = $journalDirectory . '/.' . $kind . '-scope.transaction';
        return ['target' => $journalDirectory . '/' . $kind . '-scope.json',
            'lock' => $prefix . '.lock', 0 => $prefix . '.0.json', 1 => $prefix . '.1.json'];
    };
    $journalRequest = static function (string $kind, string $bytes) use ($journalDirectory): array {
        if ($kind === 'target-sync') {
            $path = $journalDirectory . '/target-sync-scope.json';
            $expected = is_file($path) && !is_link($path) ? file_get_contents($path) : null;
            if ($expected === false || $expected === '') $expected = null;
            return [
                'schema_version' => 1, 'operation' => 'scope-journal-compare-and-swap',
                'journal' => 'target-sync',
                'expected' => $expected === null ? ['state' => 'missing'] : [
                    'state' => 'present', 'body_base64' => base64_encode($expected),
                    'sha256' => hash('sha256', $expected),
                ],
                'desired' => ['body_base64' => base64_encode($bytes),
                    'sha256' => hash('sha256', $bytes)],
            ];
        }
        return ['schema_version' => 1, 'operation' => 'scope-journal-write',
            'journal' => $kind, 'body_base64' => base64_encode($bytes)];
    };
    $journalReadRequest = static fn (string $kind): array => [
        'schema_version' => 1, 'operation' => 'scope-journal-read', 'journal' => $kind,
    ];
    $journalCasRequest = static function (?string $expected, string $desired): array {
        return [
            'schema_version' => 1, 'operation' => 'scope-journal-compare-and-swap',
            'journal' => 'target-sync',
            'expected' => $expected === null ? ['state' => 'missing'] : [
                'state' => 'present', 'body_base64' => base64_encode($expected),
                'sha256' => hash('sha256', $expected),
            ],
            'desired' => ['body_base64' => base64_encode($desired),
                'sha256' => hash('sha256', $desired)],
        ];
    };
    $resetJournal = static function (string $kind) use ($journalFiles): void {
        foreach ($journalFiles($kind) as $path) {
            if (is_dir($path) && !is_link($path)) @rmdir($path); else @unlink($path);
        }
    };
    $decodeSlot = static function (string $path): array {
        return json_decode((string) file_get_contents($path), true, 16, JSON_THROW_ON_ERROR);
    };
    foreach (['filter-migration.json', 'filter-maintenance.json'] as $managedArtifact) {
        $artifactPath = $journalDirectory . '/' . $managedArtifact;
        file_put_contents($artifactPath, "{\"schema_version\":1}\n");
        chmod($artifactPath, 0600);
    }
    $resetJournal('target-sync');
    $casOld = "{\"schema_version\":1,\"phase\":\"active\"}\n";
    $casNew = "{\"schema_version\":1,\"phase\":\"committed\"}\n";
    $casMissing = runHelper($helper, $journalCasRequest(null, $casOld));
    $casMissingResponse = json_decode($casMissing['stdout'], true, 8, JSON_THROW_ON_ERROR);
    check($casMissing['code'] === 0 && $casMissingResponse === [
        'schema_version' => 1, 'status' => 'changed'],
        'target-sync CAS must atomically create from exact missing state');
    $casAlready = runHelper($helper, $journalCasRequest(null, $casOld));
    check($casAlready['code'] === 0 && json_decode(
        $casAlready['stdout'], true, 8, JSON_THROW_ON_ERROR
    ) === ['schema_version' => 1, 'status' => 'already_applied'],
        'target-sync CAS must classify exact desired readback as already applied');
    $casChanged = runHelper($helper, $journalCasRequest($casOld, $casNew));
    check($casChanged['code'] === 0 && json_decode(
        $casChanged['stdout'], true, 8, JSON_THROW_ON_ERROR
    ) === ['schema_version' => 1, 'status' => 'changed'],
        'target-sync CAS must replace only the exact present expected body');
    $casConflict = runHelper($helper, $journalCasRequest($casOld, $casNew . " "));
    check($casConflict['code'] === 0 && json_decode(
        $casConflict['stdout'], true, 8, JSON_THROW_ON_ERROR
    ) === ['schema_version' => 1, 'status' => 'conflict'],
        'target-sync CAS must report a strict conflict without body or hash output');
    checkFixedHealthFailure(runHelper($helper, [
        'schema_version' => 1, 'operation' => 'scope-journal-write',
        'journal' => 'target-sync', 'body_base64' => base64_encode($casOld),
    ]),
        'unconditional target-sync journal writes must be rejected');
    $resetJournal('target-sync');
    $casPrior = "{\"schema_version\":0,\"phase\":\"prior\"}\n";
    file_put_contents($journalFiles('target-sync')['target'], $casPrior);
    chmod($journalFiles('target-sync')['target'], 0600);
    checkFixedHealthFailure(runHelper($helper, $journalCasRequest($casPrior, $casOld), [
        'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_CALL' => '1',
        'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_BYTES' => '31',
    ]), 'present-expected bootstrap partial rewrite must stop');
    $beforeWrongExpected = [];
    foreach ($journalFiles('target-sync') as $name => $artifact) {
        $beforeWrongExpected[$name] = is_file($artifact) ? hash_file('sha256', $artifact) : null;
    }
    checkFixedHealthFailure(runHelper($helper,
        $journalCasRequest("{\"wrong\":true}\n", $casOld)),
        'wrong present expected must not authorize bootstrap repair');
    foreach ($journalFiles('target-sync') as $name => $artifact) {
        check($beforeWrongExpected[$name] === (is_file($artifact)
            ? hash_file('sha256', $artifact) : null),
            'wrong present expected must leave every retained artifact unchanged');
    }
    $presentResume = runHelper($helper, $journalCasRequest($casPrior, $casOld));
    check($presentResume['code'] === 0 && json_decode(
        $presentResume['stdout'], true, 8, JSON_THROW_ON_ERROR
    ) === ['schema_version' => 1, 'status' => 'changed']
        && file_get_contents($journalFiles('target-sync')['target']) === $casOld,
        'exact present expected must resume the no-generation strict-prefix bootstrap');
    $resetJournal('target-sync');
    $coexistenceBody = "{\"schema_version\":2,\"coexistence\":true}\n";
    $coexistenceWrite = runHelper(
        $helper, $journalRequest('target-sync', $coexistenceBody));
    check($coexistenceWrite['code'] === 0,
        'target-sync scope journal write must accept both managed filter transaction artifacts');
    $coexistenceRead = runHelper($helper, $journalReadRequest('target-sync'));
    $coexistenceResponse = json_decode(
        $coexistenceRead['stdout'], true, 16, JSON_THROW_ON_ERROR);
    check($coexistenceRead['code'] === 0
        && ($coexistenceResponse['state'] ?? null) === 'present'
        && base64_decode((string) ($coexistenceResponse['body_base64'] ?? ''), true)
            === $coexistenceBody,
        'target-sync scope journal read must accept both managed filter transaction artifacts');
    foreach (['target-sync', 'filter'] as $kind) {
        $resetJournal($kind);
        $paths = $journalFiles($kind);
        $first = runHelper($helper, $journalRequest($kind, $journalBody));
        check($first['code'] === 0 && file_get_contents($paths['target']) === $journalBody,
            $kind . ' first persistent-slot write must succeed');
        foreach ($paths as $path) check(is_file($path) && !is_link($path)
            && fileperms($path) % 01000 === 0600, $kind . ' persistent artifacts must be 0600 files');
        $slot0Identity = stat($paths[0]); $slot1Identity = stat($paths[1]);
        $slot0 = $decodeSlot($paths[0]); $slot1 = $decodeSlot($paths[1]);
        check($slot0['generation'] === 2 && $slot1['generation'] === 1,
            $kind . ' first returned write must publish durable completion evidence');
        check($slot0['phase'] === 'complete' && $slot1['phase'] === 'bootstrap',
            $kind . ' first returned pair must retain bootstrap history under a complete active slot');
        foreach ([$slot0, $slot1] as $slotRecord) {
            foreach (['lock_dev', 'lock_ino', 'target_dev', 'target_ino'] as $boundField) {
                check(is_int($slotRecord[$boundField] ?? null),
                    $kind . ' slot must durably bind ' . $boundField);
            }
            $claimed = $slotRecord['record_sha256']; unset($slotRecord['record_sha256']);
            ksort($slotRecord, SORT_STRING);
            check(hash('sha256', json_encode($slotRecord, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR)) === $claimed,
                $kind . ' slot record checksum must cover canonical fields');
        }
        $secondBody = "{\"schema_version\":3}\n";
        check(runHelper($helper, $journalRequest($kind, $secondBody))['code'] === 0
            && file_get_contents($paths['target']) === $secondBody,
            $kind . ' second generation must commit');
        check(stat($paths[0])['ino'] === $slot0Identity['ino']
            && stat($paths[1])['ino'] === $slot1Identity['ino'],
            $kind . ' persistent slots must retain inode identity');
        check($decodeSlot($paths[0])['generation'] === 4
            && $decodeSlot($paths[1])['generation'] === 3,
            $kind . ' next write must publish bootstrap then complete generations');
        $read = runHelper($helper, $journalReadRequest($kind));
        check($read['code'] === 0 && base64_decode(json_decode(
            $read['stdout'], true, 8, JSON_THROW_ON_ERROR
        )['body_base64'], true) === $secondBody, $kind . ' read must return selected intent');
    }

    foreach (['target-sync', 'filter'] as $kind) {
        foreach ([1, 2, 3] as $partialCall) {
            $resetJournal($kind); $paths = $journalFiles($kind);
            $intended = "{\"schema_version\":" . (10 + $partialCall) . "}\n";
            checkFixedHealthFailure(runHelper($helper, $journalRequest($kind, $intended), [
                'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_CALL' => (string) $partialCall,
                'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_BYTES' => '31',
            ]), $kind . ' bootstrap partial rewrite ' . $partialCall . ' must stop');
            $restart = runHelper($helper, $journalRequest($kind, $intended));
            check($restart['code'] === 0 && file_get_contents($paths['target']) === $intended,
                $kind . ' zero/exact-prefix bootstrap remnant ' . $partialCall . ' must resume');
        }
        $resetJournal($kind); $paths = $journalFiles($kind);
        checkFixedHealthFailure(runHelper($helper, $journalRequest($kind, $journalBody), [
            'PRIVATE_CONFIG_TEST_JOURNAL_CRASH' => 'missing-target-created',
        ]), $kind . ' crash after empty target precreation must stop');
        $missingRead = runHelper($helper, $journalReadRequest($kind));
        check($missingRead['code'] === 0 && json_decode(
            $missingRead['stdout'], true, 8, JSON_THROW_ON_ERROR
        ) === ['schema_version' => 1, 'state' => 'missing'],
            $kind . ' empty target with empty slots must read as logical missing');
        check(runHelper($helper, $journalRequest($kind, $journalBody))['code'] === 0,
            $kind . ' first write must restart from the empty bound target');
        $restartedSlot0 = $decodeSlot($paths[0]);
        check($restartedSlot0['prior_target_exists'] === false
            && is_int($restartedSlot0['target_dev']) && is_int($restartedSlot0['target_ino']),
            $kind . ' restarted empty target must be logically missing but inode-bound');
        foreach ([4, 5] as $partialCall) {
            $resetJournal($kind); $paths = $journalFiles($kind);
            $intended = "{\"schema_version\":" . (10 + $partialCall) . "}\n";
            checkFixedHealthFailure(runHelper($helper, $journalRequest($kind, $intended), [
                'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_CALL' => (string) $partialCall,
                'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_BYTES' => '31',
            ]), $kind . ' first completion partial rewrite ' . $partialCall . ' must stop');
            $restart = runHelper($helper, $journalReadRequest($kind));
            check($restart['code'] === 0 && file_get_contents($paths['target']) === $intended
                && base64_decode(json_decode($restart['stdout'], true, 8,
                    JSON_THROW_ON_ERROR)['body_base64'], true) === $intended,
                $kind . ' bound complete-slot/high-water prefix ' . $partialCall . ' must resume');
        }
        $resetJournal($kind); $paths = $journalFiles($kind);
        check(runHelper($helper, $journalRequest($kind, $journalBody))['code'] === 0,
            $kind . ' post-generation fixture must initialize');
        $newBody = "{\"schema_version\":20}\n";
        checkFixedHealthFailure(runHelper($helper, $journalRequest($kind, $newBody), [
            'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_CALL' => '1',
            'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_BYTES' => '43',
        ]), $kind . ' partial post-generation slot rewrite must stop');
        checkFixedHealthFailure(runHelper($helper, $journalReadRequest($kind)),
            $kind . ' post-generation invalid slot must fail closed');

        $resetJournal($kind); $paths = $journalFiles($kind);
        check(runHelper($helper, $journalRequest($kind, $journalBody))['code'] === 0,
            $kind . ' durable-pair zero-slot fixture must initialize');
        file_put_contents($paths[0], ''); chmod($paths[0], 0600);
        checkFixedHealthFailure(runHelper($helper, $journalReadRequest($kind)),
            $kind . ' valid generation 1 must not adopt a zeroed generation 0 after a durable pair');

        $resetJournal($kind); $paths = $journalFiles($kind);
        check(runHelper($helper, $journalRequest($kind, $journalBody))['code'] === 0,
            $kind . ' target-repair fixture must initialize');
        checkFixedHealthFailure(runHelper($helper, $journalRequest($kind, $newBody), [
            'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_CALL' => '2',
            'PRIVATE_CONFIG_TEST_JOURNAL_PARTIAL_BYTES' => '7',
        ]), $kind . ' partial retained-target rewrite must stop');
        check(runHelper($helper, $journalReadRequest($kind))['code'] === 0
            && file_get_contents($paths['target']) === $newBody,
            $kind . ' checksum-valid generation must repair exact target prefix');
    }

    $targetPaths = $journalFiles('target-sync');
    $filterPaths = $journalFiles('filter');
    $resetJournal('target-sync'); $resetJournal('filter');
    check(runHelper($helper, $journalRequest('target-sync', $journalBody))['code'] === 0
        && runHelper($helper, $journalRequest('filter', $journalBody))['code'] === 0,
        'both journal kinds must initialize independently');
    copy($targetPaths[0], $filterPaths[0]); chmod($filterPaths[0], 0600);
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('filter')),
        'cross-kind slot substitution on the retained slot inode must fail closed');
    check(runHelper($helper, $journalReadRequest('target-sync'))['code'] === 0,
        'cross-kind substitution must not affect the other kind');

    $resetJournal('target-sync'); $paths = $journalFiles('target-sync');
    check(runHelper($helper, $journalRequest('target-sync', $journalBody))['code'] === 0,
        'cross-inode slot fixture must initialize');
    $originalSlotBytes = (string) file_get_contents($paths[0]);
    $replacementSlot = $home . '/replacement-slot';
    file_put_contents($replacementSlot, $originalSlotBytes); chmod($replacementSlot, 0600);
    unlink($paths[0]); rename($replacementSlot, $paths[0]);
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('target-sync')),
        'same canonical slot bytes on a replacement inode must fail closed');

    foreach (['foreign bytes' => "foreign\n", 'valid JSON foreign' => "{\"generation\":0}\n"]
             as $case => $foreign) {
        $resetJournal('target-sync'); $paths = $journalFiles('target-sync');
        check(runHelper($helper, $journalReadRequest('target-sync'))['code'] === 0,
            'empty persistent artifacts must be creatable');
        file_put_contents($paths[0], $foreign); chmod($paths[0], 0600);
        checkFixedHealthFailure(runHelper($helper, $journalRequest('target-sync', $journalBody)),
            $case . ' must not be adopted as bootstrap state');
        check(file_get_contents($paths[0]) === $foreign,
            $case . ' must remain untouched on fail-closed bootstrap');
    }

    $resetJournal('target-sync'); $paths = $journalFiles('target-sync');
    check(runHelper($helper, $journalRequest('target-sync', $journalBody))['code'] === 0,
        'generation tamper fixture must initialize');
    $generationRecord = $decodeSlot($paths[0]);
    $generationRecord['generation'] = 4; unset($generationRecord['record_sha256']);
    ksort($generationRecord, SORT_STRING);
    $generationRecord['record_sha256'] = hash('sha256', json_encode(
        $generationRecord, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR
    )); ksort($generationRecord, SORT_STRING);
    file_put_contents($paths[0], json_encode($generationRecord,
        JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n"); chmod($paths[0], 0600);
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('target-sync')),
        'nonconsecutive checksum-valid generations must fail closed');

    $resetJournal('target-sync'); $paths = $journalFiles('target-sync');
    check(runHelper($helper, $journalRequest('target-sync', $journalBody))['code'] === 0,
        'equal-generation fixture must initialize');
    $equalRecord = $decodeSlot($paths[1]);
    $equalRecord['generation'] = 2; unset($equalRecord['record_sha256']);
    ksort($equalRecord, SORT_STRING);
    $equalRecord['record_sha256'] = hash('sha256', json_encode(
        $equalRecord, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR));
    ksort($equalRecord, SORT_STRING);
    file_put_contents($paths[1], json_encode($equalRecord,
        JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n"); chmod($paths[1], 0600);
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('target-sync')),
        'equal checksum-valid generation claims must fail closed');

    $resetJournal('target-sync'); $paths = $journalFiles('target-sync');
    check(runHelper($helper, $journalRequest('target-sync', $journalBody))['code'] === 0,
        'checksum tamper fixture must initialize');
    $checksumRecord = $decodeSlot($paths[0]); $checksumRecord['intended_sha256'] = str_repeat('0', 64);
    file_put_contents($paths[0], json_encode($checksumRecord,
        JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n"); chmod($paths[0], 0600);
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('target-sync')),
        'slot checksum mismatch must fail closed');

    foreach (['target-sync', 'filter'] as $kind) {
        $resetJournal($kind); $paths = $journalFiles($kind);
        check(runHelper($helper, $journalRequest($kind, $journalBody))['code'] === 0,
            $kind . ' unsafe-artifact fixture must initialize');
        chmod($paths[0], 0644);
        checkFixedHealthFailure(runHelper($helper, $journalReadRequest($kind)),
            $kind . ' wrong-mode slot must fail closed');
        chmod($paths[0], 0600);
        $saved = $home . '/' . $kind . '-saved-slot'; rename($paths[0], $saved); link($saved, $paths[0]);
        checkFixedHealthFailure(runHelper($helper, $journalReadRequest($kind)),
            $kind . ' hardlinked slot must fail closed');
        unlink($paths[0]); rename($saved, $paths[0]);
        chmod($paths['lock'], 0644);
        checkFixedHealthFailure(runHelper($helper, $journalReadRequest($kind)),
            $kind . ' wrong-mode persistent lock must fail closed');
        chmod($paths['lock'], 0600);
    }

    $paths = $journalFiles('target-sync');
    $oldArtifact = $journalDirectory . '/.target-sync-scope.transaction.json';
    file_put_contents($oldArtifact, "old\n"); chmod($oldArtifact, 0600);
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('target-sync')),
        'legacy transaction artifact must fail closed without deletion');
    check(file_get_contents($oldArtifact) === "old\n", 'legacy artifact must never be deleted');
    unlink($oldArtifact);

    $runningJournalFile = startJournalHelper($helper,
        $journalRequest('target-sync', "{\"schema_version\":30}\n"),
        'PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE', 'target-opened');
    check(fread($runningJournalFile['pipes'][3], 1) === 'R', 'retained target race barrier');
    fclose($runningJournalFile['pipes'][3]);
    $savedTarget = $home . '/saved-journal-target'; rename($paths['target'], $savedTarget);
    $foreignTarget = "{\"schema_version\":99}\n";
    file_put_contents($paths['target'], $foreignTarget); chmod($paths['target'], 0600);
    fwrite($runningJournalFile['pipes'][4], 'C'); fclose($runningJournalFile['pipes'][4]);
    checkFixedHealthFailure(finishHelper($runningJournalFile),
        'same-owner retained-target pathname replacement must fail closed');
    check(file_get_contents($paths['target']) === $foreignTarget,
        'interposed target must not be overwritten or deleted');
    unlink($paths['target']); rename($savedTarget, $paths['target']);

    $runningJournalParent = startJournalHelper($helper,
        $journalRequest('target-sync', "{\"schema_version\":31}\n"),
        'PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE', 'target-opened');
    check(fread($runningJournalParent['pipes'][3], 1) === 'R', 'journal parent race barrier');
    fclose($runningJournalParent['pipes'][3]);
    rename($journalDirectory, $journalDirectory . '-old'); mkdir($journalDirectory, 0700);
    fwrite($runningJournalParent['pipes'][4], 'C'); fclose($runningJournalParent['pipes'][4]);
    checkFixedHealthFailure(finishHelper($runningJournalParent),
        'same-owner journal parent replacement must fail closed');
    rmdir($journalDirectory); rename($journalDirectory . '-old', $journalDirectory);

    $paths = $journalFiles('target-sync');
    $beforeLockRaceTarget = (string) file_get_contents($paths['target']);
    $runningJournalLock = startJournalHelper($helper,
        $journalRequest('target-sync', "{\"schema_version\":32}\n"),
        'PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE', 'lock-held');
    check(fread($runningJournalLock['pipes'][3], 1) === 'R',
        'retained journal lock race barrier');
    fclose($runningJournalLock['pipes'][3]);
    $savedLock = $home . '/saved-journal-lock';
    rename($paths['lock'], $savedLock);
    file_put_contents($paths['lock'], ''); chmod($paths['lock'], 0600);
    clearstatcache(true, $paths['lock']); clearstatcache(true, $savedLock);
    check(stat($paths['lock'])['ino'] !== stat($savedLock)['ino'],
        'replacement journal lock fixture must use a distinct inode');
    checkFixedHealthFailure(runHelper($helper, $journalReadRequest('target-sync'), [
        'PRIVATE_CONFIG_TEST_JOURNAL_NONBLOCK' => '1',
        'PRIVATE_CONFIG_TEST_BYPASS_OUTER_LOCK' => '1',
    ]),
        'second process locking a replacement journal lock must fail bound-slot validation');
    fwrite($runningJournalLock['pipes'][4], 'C'); fclose($runningJournalLock['pipes'][4]);
    checkFixedHealthFailure(finishHelper($runningJournalLock),
        'original journal lock holder must fail after pathname replacement');
    check(file_get_contents($paths['target']) === $beforeLockRaceTarget,
        'split journal lock processes must not mutate the target');
    unlink($paths['lock']); rename($savedLock, $paths['lock']);

    foreach (['zero' => '', 'prefix' => substr($journalBody, 0, 7)] as $case => $foreign) {
        $resetJournal('filter'); $paths = $journalFiles('filter');
        $runningMissingTarget = startJournalHelper($helper,
            $journalRequest('filter', $journalBody),
            'PRIVATE_CONFIG_TEST_JOURNAL_INTERPOSE', 'missing-target-created');
        check(fread($runningMissingTarget['pipes'][3], 1) === 'R',
            $case . ' missing-origin target race barrier');
        fclose($runningMissingTarget['pipes'][3]);
        $savedMissingTarget = $home . '/saved-missing-target-' . $case;
        rename($paths['target'], $savedMissingTarget);
        file_put_contents($paths['target'], $foreign); chmod($paths['target'], 0600);
        fwrite($runningMissingTarget['pipes'][4], 'C');
        fclose($runningMissingTarget['pipes'][4]);
        checkFixedHealthFailure(finishHelper($runningMissingTarget),
            $case . ' interposition after missing-target creation must fail closed');
        check(file_get_contents($paths['target']) === $foreign,
            $case . ' interposed missing-origin target must never be overwritten');
        unlink($paths['target']); rename($savedMissingTarget, $paths['target']);
    }

    checkFixedHealthFailure(runHelper($helper, [
        'schema_version' => 1, 'operation' => 'scope-journal-read',
        'journal' => 'arbitrary',
    ]), 'arbitrary scope journal kinds must fail closed');
    checkFixedHealthFailure(runHelper($helper, [
        'schema_version' => 1, 'operation' => 'scope-journal-read',
        'journal' => 'target-sync',
    ], ['PRIVATE_CONFIG_TEST_JOURNAL_WRONG_OWNER' => '1']),
    'wrong-owner scope journal must fail closed');
    chmod($journalDirectory, 0755);
    checkFixedHealthFailure(runHelper($helper, [
        'schema_version' => 1, 'operation' => 'scope-journal-read',
        'journal' => 'target-sync',
    ]), 'unsafe scope journal parent mode must fail closed');
    chmod($journalDirectory, 0700);
    checkFixedHealthFailure(runHelper($helper, [
        'schema_version' => 1, 'operation' => 'scope-journal-read',
        'journal' => 'target-sync',
    ], ['PRIVATE_CONFIG_TEST_JOURNAL_PARENT_WRONG_OWNER' => '1']),
    'wrong-owner scope journal parent must fail closed');
    check(runHelper($helper, [
        'schema_version' => 1, 'operation' => 'scope-journal-read',
        'journal' => 'target-sync',
    ])['code'] === 0, 'scope journal must recover after synthetic owner checks');
    $healthReal = $home . '/health-real';
    mkdir($healthReal . '/nested', 0700, true);
    symlink($healthReal, $home . '/health-link');
    $unsafeHealthConfig = $old;
    $unsafeHealthConfig['log_path'] = $home . '/health-link/nested/notifier.jsonl';
    writeConfig($configPath, $unsafeHealthConfig);
    checkFixedHealthFailure(runHelper(
        $helper, ['schema_version' => 1, 'operation' => 'health-summary']
    ), 'unsafe parent-chain symlink must fail with the fixed redacted response');
    unlink($home . '/health-link');
    writeConfig($configPath, $old);
    $healthPath = $configDirectory . '/delivery-health.json';
    $escapedDuplicateHealth = '{"schema_version":1,"status":"healthy",'
        . '"\\u0073tatus":"degraded","changed_at":"2026-07-13T00:00:00Z",'
        . '"classification":"transport_error","next_observation_sequence":2,'
        . '"last_applied_sequence":1,"message_id_hash":"' . str_repeat('a', 64) . '"}';
    file_put_contents($healthPath, $escapedDuplicateHealth);
    chmod($healthPath, 0600);
    checkFixedHealthFailure(runHelper(
        $helper, ['schema_version' => 1, 'operation' => 'health-summary']
    ), 'escaped-equivalent duplicate keys must fail with the fixed redacted response');
    unlink($healthPath);
    foreach ([
        ['healthy', 'success'], ['degraded', 'transport_error'],
    ] as [$healthState, $healthClassification]) {
        $storedHealth = [
            'schema_version' => 1, 'status' => $healthState,
            'changed_at' => '2026-07-13T00:00:00Z',
            'next_observation_sequence' => 2, 'last_applied_sequence' => 1,
        ];
        if ($healthState === 'degraded') {
            $storedHealth['classification'] = $healthClassification;
            $storedHealth['message_id_hash'] = str_repeat('c', 64);
        }
        file_put_contents($healthPath, json_encode(
            $storedHealth, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR
        ));
        chmod($healthPath, 0600);
        $healthResponse = runHelper(
            $helper, ['schema_version' => 1, 'operation' => 'health-summary']
        );
        check($healthResponse['code'] === 0, $healthState . ' health summary must succeed');
        $healthDecoded = json_decode($healthResponse['stdout'], true, 16, JSON_THROW_ON_ERROR);
        check(!array_key_exists('message_id_hash', $healthDecoded)
            && $healthDecoded['state'] === $healthState
            && $healthDecoded['classification'] === $healthClassification,
            $healthState . ' health summary must expose only redacted fields');
        unlink($healthPath);
    }

    $validHealthFixture = [
        'schema_version' => 1, 'status' => 'degraded',
        'changed_at' => '2026-07-13T00:00:00Z', 'classification' => 'transport_error',
        'next_observation_sequence' => 2, 'last_applied_sequence' => 1,
        'message_id_hash' => str_repeat('d', 64),
    ];
    $invalidHealthBytes = [
        'ordinary duplicate keys' => '{"schema_version":1,"status":"healthy",'
            . '"status":"degraded","changed_at":"2026-07-13T00:00:00Z",'
            . '"classification":"transport_error","next_observation_sequence":2,'
            . '"last_applied_sequence":1,"message_id_hash":"' . str_repeat('d', 64) . '"}',
        'unknown key' => json_encode(
            [...$validHealthFixture, 'unknown' => true], JSON_THROW_ON_ERROR
        ),
        'missing key' => json_encode(array_diff_key(
            $validHealthFixture, ['classification' => true]
        ), JSON_THROW_ON_ERROR),
        'invalid timestamp' => json_encode(
            [...$validHealthFixture, 'changed_at' => '2026-02-30T00:00:00Z'],
            JSON_THROW_ON_ERROR
        ),
        'invalid sequence ordering' => json_encode(
            [...$validHealthFixture, 'next_observation_sequence' => 1,
                'last_applied_sequence' => 2], JSON_THROW_ON_ERROR
        ),
        'invalid message hash' => json_encode(
            [...$validHealthFixture, 'message_id_hash' => str_repeat('A', 64)],
            JSON_THROW_ON_ERROR
        ),
        'invalid classification' => json_encode(
            [...$validHealthFixture, 'classification' => 'not_allowlisted'],
            JSON_THROW_ON_ERROR
        ),
        'healthy with degraded-only keys' => json_encode([
            ...$validHealthFixture, 'status' => 'healthy', 'classification' => 'success'
        ], JSON_THROW_ON_ERROR),
        '4097-byte file' => str_repeat('x', 4097),
    ];
    foreach ($invalidHealthBytes as $fixtureName => $fixtureBytes) {
        file_put_contents($healthPath, $fixtureBytes);
        chmod($healthPath, 0600);
        checkFixedHealthFailure(runHelper(
            $helper, ['schema_version' => 1, 'operation' => 'health-summary']
        ), $fixtureName . ' must fail with the fixed redacted response');
        unlink($healthPath);
    }
    file_put_contents($healthPath, json_encode($validHealthFixture, JSON_THROW_ON_ERROR));
    chmod($healthPath, 0644);
    checkFixedHealthFailure(runHelper(
        $helper, ['schema_version' => 1, 'operation' => 'health-summary']
    ), 'wrong-mode health file must fail with the fixed redacted response');
    unlink($healthPath);
    $hardlinkSource = $configDirectory . '/health-hardlink-source.json';
    file_put_contents($hardlinkSource, json_encode($validHealthFixture, JSON_THROW_ON_ERROR));
    chmod($hardlinkSource, 0600);
    link($hardlinkSource, $healthPath);
    checkFixedHealthFailure(runHelper(
        $helper, ['schema_version' => 1, 'operation' => 'health-summary']
    ), 'hardlinked health file must fail with the fixed redacted response');
    unlink($healthPath);
    unlink($hardlinkSource);
    $symlinkTarget = $configDirectory . '/health-symlink-target.json';
    file_put_contents($symlinkTarget, json_encode($validHealthFixture, JSON_THROW_ON_ERROR));
    chmod($symlinkTarget, 0600);
    symlink($symlinkTarget, $healthPath);
    checkFixedHealthFailure(runHelper(
        $helper, ['schema_version' => 1, 'operation' => 'health-summary']
    ), 'direct health symlink must fail with the fixed redacted response');
    unlink($healthPath);
    unlink($symlinkTarget);
    file_put_contents($healthPath, json_encode($validHealthFixture, JSON_THROW_ON_ERROR));
    chmod($healthPath, 0600);
    checkFixedHealthFailure(runHelper(
        $helper, ['schema_version' => 1, 'operation' => 'health-summary'],
        ['PRIVATE_CONFIG_TEST_WRONG_OWNER' => '1']
    ), 'wrong-owner health file must fail with the fixed redacted response');
    unlink($healthPath);

    $raceHealthParent = $home . '/health-race';
    $raceHealthReplacement = $home . '/health-race-replacement';
    mkdir($raceHealthParent, 0700);
    mkdir($raceHealthReplacement, 0700);
    $raceConfig = $old;
    $raceConfig['log_path'] = $raceHealthParent . '/notifier.jsonl';
    writeConfig($configPath, $raceConfig);
    $validHealth = json_encode([
        'schema_version' => 1, 'status' => 'healthy',
        'changed_at' => '2026-07-13T00:00:00Z',
        'next_observation_sequence' => 1, 'last_applied_sequence' => 1,
    ], JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
    file_put_contents($raceHealthParent . '/delivery-health.json', $validHealth);
    chmod($raceHealthParent . '/delivery-health.json', 0600);
    $runningHealth = startHealthHelper($helper);
    check(fread($runningHealth['pipes'][3], 1) === 'R',
        'health helper must reach post-read race barrier');
    fclose($runningHealth['pipes'][3]);
    rename($raceHealthParent, $raceHealthParent . '-moved');
    symlink($raceHealthReplacement, $raceHealthParent);
    fwrite($runningHealth['pipes'][4], 'C');
    fclose($runningHealth['pipes'][4]);
    $healthRaceResult = finishHelper($runningHealth);
    checkFixedHealthFailure($healthRaceResult,
        'post-read parent replacement must fail with the fixed redacted response');
    unlink($raceHealthParent);
    rename($raceHealthParent . '-moved', $raceHealthParent);
    $runningFileRace = startHealthHelper($helper);
    check(fread($runningFileRace['pipes'][3], 1) === 'R',
        'health helper must reach post-read file race barrier');
    fclose($runningFileRace['pipes'][3]);
    rename($raceHealthParent . '/delivery-health.json',
        $raceHealthParent . '/delivery-health-old.json');
    file_put_contents($raceHealthParent . '/delivery-health.json', $validHealth);
    chmod($raceHealthParent . '/delivery-health.json', 0600);
    fwrite($runningFileRace['pipes'][4], 'C');
    fclose($runningFileRace['pipes'][4]);
    $healthFileRaceResult = finishHelper($runningFileRace);
    checkFixedHealthFailure($healthFileRaceResult,
        'post-read file replacement must fail with the fixed redacted response');
    writeConfig($configPath, $old);

    foreach ([
        ['count-32', helperBoundaryAddresses(32), $old['log_path'], true],
        ['count-33', helperBoundaryAddresses(33), $old['log_path'], false],
        ['to-900', helperBoundaryAddresses(32, 900), $old['log_path'], true],
        ['to-901', helperBoundaryAddresses(32, 901), $old['log_path'], false],
        ['log-4096', ['operator@example.invalid'], '/' . str_repeat('a', 4095), true],
        ['log-4097', ['operator@example.invalid'], '/' . str_repeat('a', 4096), false],
    ] as [$boundaryName, $boundaryRecipients, $boundaryLogPath, $boundaryAccepted]) {
        $boundaryConfig = $old;
        $boundaryConfig['error_recipients'] = $boundaryRecipients;
        $boundaryConfig['log_path'] = $boundaryLogPath;
        writeConfig($configPath, $boundaryConfig);
        $boundaryRead = runHelper($helper, ['schema_version' => 1, 'operation' => 'read']);
        check(($boundaryRead['code'] === 0) === $boundaryAccepted,
            $boundaryName . ' helper boundary classification must match');
    }
    $targetHeavyConfig = $old;
    $targetHeavyConfig['notification_pinned_targets'] = helperBoundaryAddresses(40);
    $targetHeavyConfig['notification_targets'] = helperBoundaryAddresses(40);
    writeConfig($configPath, $targetHeavyConfig);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] === 0,
        'More than 32 notification targets must be accepted when error recipients fit');
    writeConfig($configPath, $old);

    $lockPath = $configDirectory . '/.manage-private-config.lock';
    check(is_file($lockPath) && !is_link($lockPath)
        && fileperms($lockPath) % 01000 === 0600, 'lock must be owner-only regular file');
    chmod($lockPath, 0644);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'wrong-mode lock must be rejected');
    chmod($lockPath, 0600);
    unlink($lockPath);
    $outsideLock = $temporary . '/outside-lock';
    file_put_contents($outsideLock, 'x');
    chmod($outsideLock, 0600);
    symlink($outsideLock, $lockPath);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'symlink lock must be rejected');
    unlink($lockPath);

    $updated = $old;
    $updated['webhook_url'] = 'https://webhook.worksmobile.com/message/secret-token';
    $changed = runHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
        'expected_sha256' => hash('sha256', $oldBytes), 'config' => $updated]);
    check($changed['code'] === 0, 'matching CAS must succeed');
    $result = json_decode($changed['stdout'], true, 16, JSON_THROW_ON_ERROR);
    check($result['status'] === 'changed', 'CAS must change matching old bytes');
    check(fileperms($configPath) % 01000 === 0600, 'config must remain 0600');
    check(!str_contains($changed['stdout'] . $changed['stderr'], 'secret-token'), 'output must not leak');

    $currentBytes = file_get_contents($configPath);
    $unchanged = runHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
        'expected_sha256' => hash('sha256', $currentBytes), 'config' => $updated]);
    check(json_decode($unchanged['stdout'], true, 16, JSON_THROW_ON_ERROR)['status'] === 'unchanged',
        'equal config must be unchanged');
    $conflict = runHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
        'expected_sha256' => str_repeat('0', 64), 'config' => $updated]);
    check(json_decode($conflict['stdout'], true, 16, JSON_THROW_ON_ERROR)['status'] === 'conflict',
        'mismatching old hash must conflict');

    $missingUnknown = ['webhook_url' => $updated['webhook_url']];
    $rejected = runHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
        'expected_sha256' => hash('sha256', $currentBytes), 'config' => $missingUnknown]);
    check($rejected['code'] !== 0, 'unknown-key loss must be rejected');
    check(!str_contains($rejected['stdout'] . $rejected['stderr'], 'secret-token'), 'failure must not leak');

    chmod($configPath, 0644);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'wrong config mode must be rejected');
    chmod($configPath, 0600);
    $real = $configDirectory . '/real.json';
    rename($configPath, $real);
    symlink($real, $configPath);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'config symlink must be rejected');
    unlink($configPath);
    rename($real, $configPath);

    file_put_contents($configPath, str_repeat('x', 65537));
    chmod($configPath, 0600);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'oversize config must be rejected');
    writeConfig($configPath, $updated);
    $oversize = runHelper($helper, str_repeat('x', 131073));
    check($oversize['code'] !== 0, 'oversize request must be rejected');
    check(strlen($oversize['stdout'] . $oversize['stderr']) < 1024, 'failure output must remain bounded');

    foreach ([
        ['webhook_url' => 'http://webhook.worksmobile.com/message/token',
            'error_recipients' => ['operator@example.invalid'], 'log_path' => $updated['log_path']],
        ['webhook_url' => 'https://webhook.worksmobile.com/message/a/b',
            'error_recipients' => ['operator@example.invalid'], 'log_path' => $updated['log_path']],
        ['webhook_url' => 'https://webhook.worksmobile.com/message/token?query=1',
            'error_recipients' => ['operator@example.invalid'], 'log_path' => $updated['log_path']],
        ['webhook_url' => $updated['webhook_url'], 'error_recipients' => [],
            'log_path' => $updated['log_path']],
        ['webhook_url' => $updated['webhook_url'], 'error_recipients' => ['not-an-email'],
            'log_path' => $updated['log_path']],
        ['webhook_url' => $updated['webhook_url'], 'error_recipients' => ['operator@example.invalid'],
            'log_path' => 'relative/notifier.jsonl'],
        ['webhook_url' => $updated['webhook_url'], 'error_recipients' => ['operator@example.invalid'],
            'log_path' => '/home/example/public_html/notifier.jsonl'],
    ] as $invalidConfig) {
        writeConfig($configPath, $invalidConfig);
        check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
            'invalid notifier schema must be rejected on read');
        writeConfig($configPath, $updated);
        $invalidCas = runHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
            'expected_sha256' => hash('sha256', file_get_contents($configPath)), 'config' => $invalidConfig]);
        check($invalidCas['code'] !== 0, 'invalid notifier schema must be rejected on replacement');
    }

    $raceBase = $updated;
    $raceBytes = writeConfig($configPath, $raceBase);
    $raceA = $raceBase;
    $raceA['unknown'] = 'race-a';
    $raceB = $raceBase;
    $raceB['unknown'] = 'race-b';
    $expectedRaceHash = hash('sha256', $raceBytes);
    $barrier = fopen($lockPath, 'r+b');
    check(is_resource($barrier) && flock($barrier, LOCK_EX), 'race barrier lock must be held');
    $runningA = startHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
        'expected_sha256' => $expectedRaceHash, 'config' => $raceA]);
    $runningB = startHelper($helper, ['schema_version' => 1, 'operation' => 'compare_and_swap',
        'expected_sha256' => $expectedRaceHash, 'config' => $raceB]);
    waitHelperReady($runningA);
    waitHelperReady($runningB);
    flock($barrier, LOCK_UN);
    fclose($barrier);
    $statuses = [];
    foreach ([finishHelper($runningA), finishHelper($runningB)] as $raceResult) {
        check($raceResult['code'] === 0, 'concurrent CAS must return a classified result');
        $statuses[] = json_decode($raceResult['stdout'], true, 16, JSON_THROW_ON_ERROR)['status'];
    }
    sort($statuses);
    check($statuses === ['changed', 'conflict'], 'serialized CAS must change exactly once');

    $realParent = $home . '/mail-lineworks/real-private';
    rename($configDirectory, $realParent);
    symlink($realParent, $configDirectory);
    $symlinkParent = runHelper($helper, ['schema_version' => 1, 'operation' => 'read']);
    check($symlinkParent['code'] !== 0, 'symlinked config parent must be rejected');
    unlink($configDirectory);
    rename($realParent, $configDirectory);

    chmod($configDirectory, 0755);
    $unsafeParent = runHelper($helper, ['schema_version' => 1, 'operation' => 'read']);
    check($unsafeParent['code'] !== 0, 'wrong config parent mode must be rejected');
    chmod($configDirectory, 0700);

    $mailDirectory = dirname($configDirectory);
    $realMailDirectory = $home . '/real-mail-lineworks';
    rename($mailDirectory, $realMailDirectory);
    symlink($realMailDirectory, $mailDirectory);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'symlinked intermediate directory must be rejected');
    unlink($mailDirectory);
    rename($realMailDirectory, $mailDirectory);

    chmod($home, 0770);
    check(runHelper($helper, ['schema_version' => 1, 'operation' => 'read'])['code'] !== 0,
        'group-writable account home must be rejected');
    chmod($home, 0700);
} finally {
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($temporary, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST,
    );
    foreach ($iterator as $entry) {
        $entry->isDir() && !$entry->isLink() ? rmdir($entry->getPathname()) : unlink($entry->getPathname());
    }
    rmdir($temporary);
}

echo "PASS: private config helper tests\n";
