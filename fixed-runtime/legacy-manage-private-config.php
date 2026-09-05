<?php
declare(strict_types=1);

const REQUEST_LIMIT = 262144;
const CONFIG_LIMIT = 65536;
const MAX_RECIPIENTS = 32;
const MAX_TO_BYTES = 900;
const MAX_LOG_PATH_BYTES = 4096;
const MAX_HEADER_LINE_BYTES = 997;
const MAX_SIGNED_MESSAGE_BYTES = 65535;
// Upper bounds for all non-configurable header/message bytes; runtimeConfigSizes()
// adds the configurable To and UTF-8 log-path bytes explicitly.
const SYSTEM_MAIL_FIXED_HEADER_LINE_BYTES = 160;
const SYSTEM_MAIL_FIXED_MESSAGE_BYTES = 8192;

/** @return list<string> */
function canonicalEmailList(mixed $values, bool $allowEmpty): array
{
    if (!is_array($values) || !array_is_list($values)) {
        throw new RuntimeException();
    }
    $canonical = [];
    foreach ($values as $value) {
        if (!is_string($value) || substr_count($value, '@') !== 1 || strlen($value) > 254) {
            throw new RuntimeException();
        }
        [$local, $domain] = explode('@', $value, 2);
        $labels = explode('.', $domain);
        $localPattern = "~\\A[A-Za-z0-9!#$%&'*+/=?^_`{|}\\~-]+(?:\\.[A-Za-z0-9!#$%&'*+/=?^_`{|}\\~-]+)*\\z~D";
        $labelPattern = '~\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\z~D';
        if (strlen($local) > 64 || strlen($domain) > 253 || count($labels) < 2
            || preg_match($localPattern, $local) !== 1) {
            throw new RuntimeException();
        }
        foreach ($labels as $label) {
            if (preg_match($labelPattern, $label) !== 1) {
                throw new RuntimeException();
            }
        }
        $canonical[] = $local . '@' . strtolower($domain);
    }
    $canonical = array_values(array_unique($canonical, SORT_STRING));
    sort($canonical, SORT_STRING);
    if (!$allowEmpty && $canonical === []) {
        throw new RuntimeException();
    }
    return $canonical;
}

/** @return array{recipient_count:int,to_bytes:int,log_path_bytes:int,header_line_bytes:int,signed_message_bytes:int} */
function runtimeConfigSizes(array $recipients, array $pinned, array $targets, string $logPath): array
{
    $toBytes = strlen(implode(',', $recipients));
    $logBytes = strlen($logPath);
    return ['recipient_count' => count($recipients), 'to_bytes' => $toBytes,
        'log_path_bytes' => $logBytes,
        'header_line_bytes' => max(SYSTEM_MAIL_FIXED_HEADER_LINE_BYTES, strlen('To: ') + $toBytes),
        'signed_message_bytes' => SYSTEM_MAIL_FIXED_MESSAGE_BYTES + $toBytes + $logBytes];
}

final class ConfigConflict extends RuntimeException {}

function failClosed(): never
{
    fwrite(STDERR, "private config operation failed\n");
    exit(1);
}

/** @return array{dev: int, ino: int, uid: int, mode: int} */
function directorySnapshot(string $directory, ?int $exactMode = 0700): array
{
    clearstatcache(true, $directory);
    $info = @lstat($directory);
    if (!is_array($info) || is_link($directory) || (($info['mode'] & 0170000) !== 0040000)
        || $info['uid'] !== posix_geteuid()) {
        throw new RuntimeException();
    }
    $mode = $info['mode'] & 0777;
    if (($exactMode !== null && $mode !== $exactMode)
        || ($exactMode === null && ($mode & 0022) !== 0)) {
        throw new RuntimeException();
    }
    return ['dev' => $info['dev'], 'ino' => $info['ino'], 'uid' => $info['uid'],
        'mode' => $mode];
}

function assertSameDirectory(string $directory, array $expected, ?int $exactMode = 0700): void
{
    if (directorySnapshot($directory, $exactMode) !== $expected) {
        throw new RuntimeException();
    }
}

/** @param array<string, array{snapshot: array, mode: ?int}> $trust */
function assertDirectoryChain(array $trust): void
{
    foreach ($trust as $directory => $item) {
        assertSameDirectory($directory, $item['snapshot'], $item['mode']);
    }
}

/** @return array{0: string, 1: array<string, array{snapshot: array, mode: ?int}>} */
function scopeJournalLocation(string $home, string $journal): array
{
    $filenames = ['target-sync' => 'target-sync-scope.json', 'filter' => 'filter-scope.json'];
    if (!array_key_exists($journal, $filenames)) {
        throw new RuntimeException();
    }
    $directory = $home . '/private/xserver-mail-lineworks/deploy-transactions';
    $trust = [$home => ['snapshot' => directorySnapshot($home, null), 'mode' => null]];
    $cursor = $home;
    foreach (['private', 'xserver-mail-lineworks', 'deploy-transactions'] as $component) {
        $cursor .= '/' . $component;
        $trust[$cursor] = ['snapshot' => directorySnapshot($cursor), 'mode' => 0700];
    }
    return [$directory . '/' . $filenames[$journal], $trust];
}

function assertScopeJournalInventory(string $directory): void
{
    $allowed = [
        'target-sync-scope.json', 'filter-scope.json',
        'filter-migration.json', 'filter-maintenance.json',
    ];
    foreach (['target-sync-scope', 'filter-scope'] as $prefix) {
        $allowed[] = '.' . $prefix . '.transaction.lock';
        $allowed[] = '.' . $prefix . '.transaction.0.json';
        $allowed[] = '.' . $prefix . '.transaction.1.json';
    }
    $entries = @scandir($directory);
    if (!is_array($entries)) {
        throw new RuntimeException();
    }
    foreach ($entries as $entry) {
        if ($entry !== '.' && $entry !== '..' && !in_array($entry, $allowed, true)) {
            throw new RuntimeException();
        }
    }
}

/** @param resource $handle */
function fsyncScopeJournalFile($handle): void
{
    if (!fsync($handle)) {
        throw new RuntimeException();
    }
}

/** @param resource $directoryHandle */
function fsyncScopeJournalDirectory($directoryHandle): void
{
    if (!fsync($directoryHandle)) {
        throw new RuntimeException();
    }
}

/** @param resource $handle */
function rewriteScopeJournalDescriptor($handle, string $bytes): array
{
    if (!ftruncate($handle, 0) || !rewind($handle)) {
        throw new RuntimeException();
    }
    $offset = 0;
    while ($offset < strlen($bytes)) {
        $written = fwrite($handle, substr($bytes, $offset));
        if (!is_int($written) || $written < 1) {
            throw new RuntimeException();
        }
        $offset += $written;
    }
    if (!fflush($handle)) {
        throw new RuntimeException();
    }
    fsyncScopeJournalFile($handle);
    $stored = fstat($handle);
    if (!is_array($stored) || $stored['size'] !== strlen($bytes)) {
        throw new RuntimeException();
    }
    rewind($handle);
    $readback = stream_get_contents($handle, strlen($bytes) + 1);
    if (!is_string($readback) || !hash_equals(hash('sha256', $bytes), hash('sha256', $readback))) {
        throw new RuntimeException();
    }
    return $stored;
}

/** @param resource $handle */
function assertScopeJournalDirectoryHandle($handle, string $directory, array $parentTrust): void
{
    $opened = fstat($handle);
    $expected = $parentTrust[$directory]['snapshot'] ?? null;
    if (!is_array($opened) || !is_array($expected)
        || (($opened['mode'] & 0170000) !== 0040000)
        || $opened['dev'] !== $expected['dev'] || $opened['ino'] !== $expected['ino']
        || $opened['uid'] !== $expected['uid'] || (($opened['mode'] & 0777) !== 0700)) {
        throw new RuntimeException();
    }
    assertDirectoryChain($parentTrust);
}

/** @return array{lock:string,0:string,1:string} */
function scopeJournalPersistentSlots(string $path): array
{
    $prefix = dirname($path) . '/.' . pathinfo($path, PATHINFO_FILENAME);
    return ['lock' => $prefix . '.transaction.lock',
        0 => $prefix . '.transaction.0.json', 1 => $prefix . '.transaction.1.json'];
}

/** @param resource $directoryHandle */
function persistentJournalDirectoryCurrent($directoryHandle, string $directory,
    array $parentTrust): void
{
    assertScopeJournalDirectoryHandle($directoryHandle, $directory, $parentTrust);
}

/** @param resource $handle */
function persistentJournalFileCurrent(string $path, $handle, array $identity,
    int $limit): array
{
    $opened = fstat($handle);
    clearstatcache(true, $path);
    $current = @lstat($path);
    if (!is_array($opened) || !is_array($current) || is_link($path)
        || (($opened['mode'] & 0170777) !== 0100600) || $opened['uid'] !== posix_geteuid()
        || $opened['nlink'] !== 1 || $opened['size'] < 0 || $opened['size'] > $limit
        || $opened['dev'] !== $identity['dev'] || $opened['ino'] !== $identity['ino']
        || $current['dev'] !== $opened['dev'] || $current['ino'] !== $opened['ino']
        || $current['uid'] !== $opened['uid'] || $current['mode'] !== $opened['mode']
        || $current['nlink'] !== 1 || $current['size'] !== $opened['size']) {
        throw new RuntimeException();
    }
    return $opened;
}

/** @return array{handle:resource,identity:array} */
function persistentJournalOpenFile(string $path, int $limit, $directoryHandle,
    string $directory, array $parentTrust): array
{
    persistentJournalDirectoryCurrent($directoryHandle, $directory, $parentTrust);
    clearstatcache(true, $path);
    $before = @lstat($path);
    if (!is_array($before)) {
        $handle = @fopen($path, 'x+b');
        if (!is_resource($handle) || !chmod($path, 0600)) {
            if (is_resource($handle)) fclose($handle);
            throw new RuntimeException();
        }
        $before = fstat($handle);
        if (!is_array($before)) {
            fclose($handle);
            throw new RuntimeException();
        }
        fsyncScopeJournalFile($handle);
        fsyncScopeJournalDirectory($directoryHandle);
    } else {
        if (is_link($path) || (($before['mode'] & 0170777) !== 0100600)
            || $before['uid'] !== posix_geteuid() || $before['nlink'] !== 1
            || $before['size'] < 0 || $before['size'] > $limit) {
            throw new RuntimeException();
        }
        $handle = @fopen($path, 'r+b');
        if (!is_resource($handle)) throw new RuntimeException();
    }
    try {
        $identity = persistentJournalFileCurrent($path, $handle, $before, $limit);
        persistentJournalDirectoryCurrent($directoryHandle, $directory, $parentTrust);
    } catch (Throwable $error) {
        fclose($handle);
        throw $error;
    }
    return ['handle' => $handle, 'identity' => $identity];
}

/** @param resource $handle */
function persistentJournalReadDescriptor($handle, int $limit): string
{
    if (!rewind($handle)) throw new RuntimeException();
    $bytes = stream_get_contents($handle, $limit + 1);
    $after = fstat($handle);
    if (!is_string($bytes) || strlen($bytes) > $limit || !is_array($after)
        || strlen($bytes) !== $after['size']) throw new RuntimeException();
    return $bytes;
}

/** @return array<string,mixed> */
function persistentJournalRecord(string $journal, int $slot, int $generation,
    array $bindings, ?array $priorId, ?string $priorBytes, string $intended,
    string $phase): array
{
    if ($generation < 0 || ($generation % 2) !== $slot
        || !in_array($phase, ['bootstrap', 'complete'], true)) throw new RuntimeException();
    $record = ['generation' => $generation, 'intended_body_base64' => base64_encode($intended),
        'intended_sha256' => hash('sha256', $intended), 'journal' => $journal,
        'lock_dev' => $bindings['lock']['dev'], 'lock_ino' => $bindings['lock']['ino'],
        'phase' => $phase,
        'prior_target_dev' => $priorId['dev'] ?? null,
        'prior_target_exists' => $priorId !== null,
        'prior_target_ino' => $priorId['ino'] ?? null,
        'prior_target_sha256' => $priorBytes === null ? null : hash('sha256', $priorBytes),
        'schema_version' => 3, 'slot' => $slot,
        'slot0_dev' => $bindings['slots'][0]['dev'], 'slot0_ino' => $bindings['slots'][0]['ino'],
        'slot1_dev' => $bindings['slots'][1]['dev'], 'slot1_ino' => $bindings['slots'][1]['ino'],
        'target_dev' => $bindings['target']['dev'], 'target_ino' => $bindings['target']['ino'],
        'state' => 'intent'];
    ksort($record, SORT_STRING);
    $record['record_sha256'] = hash('sha256', json_encode(
        $record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR
    ));
    ksort($record, SORT_STRING);
    return $record;
}

function persistentJournalEncode(array $record): string
{
    ksort($record, SORT_STRING);
    return json_encode($record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
}

/** @return array<string,mixed> */
function persistentJournalDecode(string $bytes, string $journal, int $slot,
    array $bindings): array
{
    assertNoDecodedDuplicateJsonKeys($bytes);
    $record = json_decode($bytes, true, 8, JSON_THROW_ON_ERROR);
    $keys = ['generation', 'intended_body_base64', 'intended_sha256', 'journal',
        'lock_dev', 'lock_ino', 'phase',
        'prior_target_dev', 'prior_target_exists', 'prior_target_ino',
        'prior_target_sha256', 'record_sha256', 'schema_version', 'slot',
        'slot0_dev', 'slot0_ino', 'slot1_dev', 'slot1_ino', 'state',
        'target_dev', 'target_ino'];
    if (!is_array($record) || array_keys($record) !== $keys
        || $record['schema_version'] !== 3 || $record['journal'] !== $journal
        || $record['slot'] !== $slot || $record['state'] !== 'intent'
        || !in_array($record['phase'], ['bootstrap', 'complete'], true)
        || !is_int($record['generation']) || $record['generation'] < 0
        || ($record['generation'] % 2) !== $slot
        || $record['slot0_dev'] !== $bindings['slots'][0]['dev']
        || $record['slot0_ino'] !== $bindings['slots'][0]['ino']
        || $record['slot1_dev'] !== $bindings['slots'][1]['dev']
        || $record['slot1_ino'] !== $bindings['slots'][1]['ino']
        || $record['lock_dev'] !== $bindings['lock']['dev']
        || $record['lock_ino'] !== $bindings['lock']['ino']
        || $record['target_dev'] !== $bindings['target']['dev']
        || $record['target_ino'] !== $bindings['target']['ino']
        || !is_bool($record['prior_target_exists'])
        || !is_string($record['intended_body_base64'])
        || !is_string($record['intended_sha256'])
        || preg_match('/\A[a-f0-9]{64}\z/D', $record['intended_sha256']) !== 1
        || !is_string($record['record_sha256'])
        || preg_match('/\A[a-f0-9]{64}\z/D', $record['record_sha256']) !== 1) {
        throw new RuntimeException();
    }
    $intended = base64_decode($record['intended_body_base64'], true);
    if (!is_string($intended) || $intended === '' || strlen($intended) > 65536
        || base64_encode($intended) !== $record['intended_body_base64']
        || !hash_equals(hash('sha256', $intended), $record['intended_sha256'])) {
        throw new RuntimeException();
    }
    if ($record['prior_target_exists']) {
        if (!is_int($record['prior_target_dev']) || !is_int($record['prior_target_ino'])
            || !is_string($record['prior_target_sha256'])
            || preg_match('/\A[a-f0-9]{64}\z/D', $record['prior_target_sha256']) !== 1) {
            throw new RuntimeException();
        }
    } elseif ($record['prior_target_dev'] !== null || $record['prior_target_ino'] !== null
        || $record['prior_target_sha256'] !== null) throw new RuntimeException();
    $claimedRecordHash = $record['record_sha256'];
    unset($record['record_sha256']);
    ksort($record, SORT_STRING);
    $actualRecordHash = hash('sha256', json_encode(
        $record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR
    ));
    if (!hash_equals($claimedRecordHash, $actualRecordHash)) throw new RuntimeException();
    $record['record_sha256'] = $claimedRecordHash;
    ksort($record, SORT_STRING);
    if (!hash_equals(persistentJournalEncode($record), $bytes)) throw new RuntimeException();
    return $record;
}

/** @return array{active:int,record:array,records:array} */
function selectScopeJournalGeneration(array $slotBytes, string $journal,
    array $bindings): array
{
    $records = [];
    foreach ([0, 1] as $slot) {
        if ($slotBytes[$slot] === '') throw new RuntimeException();
        $records[$slot] = persistentJournalDecode($slotBytes[$slot], $journal, $slot, $bindings);
    }
    if (abs($records[0]['generation'] - $records[1]['generation']) !== 1) {
        // Only consecutive parity-bound generations have deterministic authority.
        throw new RuntimeException();
    }
    $active = $records[0]['generation'] > $records[1]['generation'] ? 0 : 1;
    return ['active' => $active, 'record' => $records[$active], 'records' => $records];
}

function persistentJournalBindings(array $context, array $targetIdentity): array
{
    return ['lock' => $context['lock']['identity'], 'slots' => [
        0 => $context['slots'][0]['identity'], 1 => $context['slots'][1]['identity'],
    ], 'target' => $targetIdentity];
}

function persistentJournalLeaseCurrent(array $context, ?array $target = null): void
{
    persistentJournalDirectoryCurrent($context['directory_handle'],
        $context['directory'], $context['parent_trust']);
    persistentJournalFileCurrent($context['paths']['lock'], $context['lock']['handle'],
        $context['lock']['identity'], 4096);
    foreach ([0, 1] as $slot) {
        persistentJournalFileCurrent($context['paths'][$slot], $context['slots'][$slot]['handle'],
            $context['slots'][$slot]['identity'], 131072);
    }
    if ($target !== null) {
        persistentJournalFileCurrent($context['path'], $target['handle'],
            $target['identity'], 65536);
    }
    persistentJournalDirectoryCurrent($context['directory_handle'],
        $context['directory'], $context['parent_trust']);
}

function persistentJournalCompletionBytes(array $context, int $generation): string
{
    if ($generation < 2) throw new RuntimeException();
    $record = ['complete_generation' => $generation,
        'lock_dev' => $context['lock']['identity']['dev'],
        'lock_ino' => $context['lock']['identity']['ino'], 'schema_version' => 1];
    ksort($record, SORT_STRING);
    $record['record_sha256'] = hash('sha256', json_encode(
        $record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR));
    ksort($record, SORT_STRING);
    return json_encode($record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
}

function persistentJournalCompletionGeneration(array $context,
    ?int $recoverableGeneration = null): ?int
{
    persistentJournalLeaseCurrent($context);
    $bytes = persistentJournalReadDescriptor($context['lock']['handle'], 4096);
    if ($bytes === '') return null;
    if ($recoverableGeneration !== null) {
        $canonical = persistentJournalCompletionBytes($context, $recoverableGeneration);
        if ($bytes !== $canonical && strlen($bytes) < strlen($canonical)
            && str_starts_with($canonical, $bytes)) {
            persistentJournalLeaseCurrent($context);
            rewriteScopeJournalDescriptor($context['lock']['handle'], $canonical);
            persistentJournalLeaseCurrent($context);
            fsyncScopeJournalDirectory($context['directory_handle']);
            persistentJournalLeaseCurrent($context);
            $bytes = persistentJournalReadDescriptor($context['lock']['handle'], 4096);
            if (!hash_equals($canonical, $bytes)) throw new RuntimeException();
        }
    }
    assertNoDecodedDuplicateJsonKeys($bytes);
    $record = json_decode($bytes, true, 8, JSON_THROW_ON_ERROR);
    $keys = ['complete_generation', 'lock_dev', 'lock_ino', 'record_sha256', 'schema_version'];
    if (!is_array($record) || array_keys($record) !== $keys
        || $record['schema_version'] !== 1 || !is_int($record['complete_generation'])
        || $record['complete_generation'] < 2
        || $record['lock_dev'] !== $context['lock']['identity']['dev']
        || $record['lock_ino'] !== $context['lock']['identity']['ino']
        || !is_string($record['record_sha256'])) throw new RuntimeException();
    $claimed = $record['record_sha256'];
    unset($record['record_sha256']);
    ksort($record, SORT_STRING);
    if (!hash_equals($claimed, hash('sha256', json_encode(
        $record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR)))) throw new RuntimeException();
    $record['record_sha256'] = $claimed;
    ksort($record, SORT_STRING);
    if (!hash_equals(json_encode($record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n",
        $bytes)) throw new RuntimeException();
    return $record['complete_generation'];
}

function persistJournalCompletionGeneration(array $context, int $generation): void
{
    $current = persistentJournalCompletionGeneration($context);
    if ($current !== null && $current > $generation) throw new RuntimeException();
    if ($current === $generation) return;
    persistentJournalLeaseCurrent($context);
    rewriteScopeJournalDescriptor($context['lock']['handle'],
        persistentJournalCompletionBytes($context, $generation));
    persistentJournalLeaseCurrent($context);
    fsyncScopeJournalDirectory($context['directory_handle']);
    persistentJournalLeaseCurrent($context);
    if (persistentJournalCompletionGeneration($context) !== $generation) {
        throw new RuntimeException();
    }
}

/** @param resource $handle */
function repairScopeJournalSlot(array $context, int $slot, string $canonical,
    bool $requirePrefix): array
{
    $path = $context['paths'][$slot];
    $handle = $context['slots'][$slot]['handle'];
    $identity = $context['slots'][$slot]['identity'];
    $current = persistentJournalReadDescriptor($handle, 131072);
    if ($current === $canonical) {
        return persistentJournalFileCurrent($path, $handle, $identity, 131072);
    }
    if ($requirePrefix && $current !== '' && $current !== $canonical
        && !(strlen($current) < strlen($canonical)
            && str_starts_with($canonical, $current))) throw new RuntimeException();
    // Crash-resume boundary: before-slot-rewrite.
    persistentJournalLeaseCurrent($context);
    $stored = rewriteScopeJournalDescriptor($handle, $canonical);
    persistentJournalLeaseCurrent($context);
    $stored = persistentJournalFileCurrent($path, $handle, $identity, 131072);
    $readback = persistentJournalReadDescriptor($handle, 131072);
    if (!hash_equals($canonical, $readback)) throw new RuntimeException();
    return $stored;
}

/**
 * There is no durable journal generation until both cross-bound slots validate.
 * Only while that remains true may a zero-length or exact strict-prefix bootstrap remnant be adopted;
 * the remnant contributes no data and is overwritten through its retained FD.
 */
function initializeScopeJournalBootstrapRemnant(array &$context, string $journal,
    array $bindings, ?array $priorId, ?string $priorBytes, string $intended): array
{
    $canonicals = [];
    foreach ([0, 1] as $slot) {
        $canonicals[$slot] = persistentJournalEncode(persistentJournalRecord(
            $journal, $slot, $slot, $bindings, $priorId, $priorBytes, $intended, 'bootstrap'
        ));
        $current = persistentJournalReadDescriptor($context['slots'][$slot]['handle'], 131072);
        if ($current !== '' && $current !== $canonicals[$slot]
            && !(strlen($current) < strlen($canonicals[$slot])
                && str_starts_with($canonicals[$slot], $current))) throw new RuntimeException();
    }
    foreach ([0, 1] as $slot) {
        repairScopeJournalSlot($context, $slot, $canonicals[$slot], true);
        fsyncScopeJournalDirectory($context['directory_handle']);
        // Crash-resume boundary: bootstrap-slot-durable.
        persistentJournalDirectoryCurrent($context['directory_handle'],
            $context['directory'], $context['parent_trust']);
    }
    return selectScopeJournalGeneration([
        persistentJournalReadDescriptor($context['slots'][0]['handle'], 131072),
        persistentJournalReadDescriptor($context['slots'][1]['handle'], 131072),
    ], $journal, $bindings);
}

/** @return ?array */
function adoptScopeJournalBootstrapRemnant(array &$context, string $journal,
    array $slotBytes, array $bindings): ?array
{
    $decoded = [];
    foreach ([0, 1] as $slot) {
        try {
            $decoded[$slot] = $slotBytes[$slot] === '' ? null
                : persistentJournalDecode($slotBytes[$slot], $journal, $slot, $bindings);
        } catch (Throwable) {
            $decoded[$slot] = null;
        }
    }
    // Slot 0 generation 0 is the only possible durable bootstrap anchor.  A
    // valid slot 1 proves slot 0 was already made durable, so it must never
    // authorize adoption of a later invalid slot 0.
    $anchor = $decoded[0] ?? null;
    if (!is_array($anchor) || $anchor['generation'] !== 0
        || $anchor['phase'] !== 'bootstrap' || $decoded[1] !== null) return null;
    $intended = base64_decode($anchor['intended_body_base64'], true);
    if (!is_string($intended)) throw new RuntimeException();
    $priorId = $anchor['prior_target_exists'] ? [
        'dev' => $anchor['prior_target_dev'], 'ino' => $anchor['prior_target_ino'],
    ] : null;
    $priorBytes = null;
    if ($priorId !== null) {
        $target = persistentJournalOpenTarget($context, false);
        if (!is_resource($target['handle']) || $target['identity']['dev'] !== $priorId['dev']
            || $target['identity']['ino'] !== $priorId['ino']
            || !hash_equals($anchor['prior_target_sha256'], hash('sha256', $target['bytes']))) {
            if (is_resource($target['handle'])) fclose($target['handle']);
            throw new RuntimeException();
        }
        $priorBytes = $target['bytes'];
        fclose($target['handle']);
    }
    foreach ([0, 1] as $slot) {
        $expected = persistentJournalEncode(persistentJournalRecord(
            $journal, $slot, $slot, $bindings, $priorId, $priorBytes, $intended, 'bootstrap'
        ));
        if ($slotBytes[$slot] !== '' && $slotBytes[$slot] !== $expected
            && !(strlen($slotBytes[$slot]) < strlen($expected)
                && str_starts_with($expected, $slotBytes[$slot]))) return null;
    }
    return initializeScopeJournalBootstrapRemnant(
        $context, $journal, $bindings, $priorId, $priorBytes, $intended
    );
}

/** @return array<string,mixed> */
function persistentJournalOpenContext(string $home, string $journal): array
{
    [$path, $parentTrust] = scopeJournalLocation($home, $journal);
    $directory = dirname($path);
    assertScopeJournalInventory($directory);
    $directoryHandle = @fopen($directory, 'rb');
    if (!is_resource($directoryHandle)) throw new RuntimeException();
    $slots = [];
    try {
        persistentJournalDirectoryCurrent($directoryHandle, $directory, $parentTrust);
        $paths = scopeJournalPersistentSlots($path);
        $lock = persistentJournalOpenFile($paths['lock'], 4096, $directoryHandle,
            $directory, $parentTrust);
        if (!flock($lock['handle'], LOCK_EX)) throw new RuntimeException();
        // Interposition boundary: retained-journal-lock-held.
        persistentJournalFileCurrent($paths['lock'], $lock['handle'], $lock['identity'], 4096);
        foreach ([0, 1] as $slot) {
            $slots[$slot] = persistentJournalOpenFile($paths[$slot], 131072,
                $directoryHandle, $directory, $parentTrust);
        }
        assertScopeJournalInventory($directory);
        return ['path' => $path, 'parent_trust' => $parentTrust, 'directory' => $directory,
            'directory_handle' => $directoryHandle, 'paths' => $paths,
            'lock' => $lock, 'slots' => $slots];
    } catch (Throwable $error) {
        foreach ($slots as $slot) if (is_resource($slot['handle'] ?? null)) fclose($slot['handle']);
        if (isset($lock) && is_resource($lock['handle'] ?? null)) fclose($lock['handle']);
        fclose($directoryHandle);
        throw $error;
    }
}

function persistentJournalCloseContext(array $context): void
{
    foreach ([0, 1] as $slot) fclose($context['slots'][$slot]['handle']);
    flock($context['lock']['handle'], LOCK_UN);
    fclose($context['lock']['handle']);
    fclose($context['directory_handle']);
}

/** @return array{handle:mixed,identity:?array,bytes:?string,existed:bool} */
function persistentJournalOpenTarget(array $context, bool $writable,
    bool $createMissing = false): array
{
    $path = $context['path'];
    persistentJournalDirectoryCurrent($context['directory_handle'],
        $context['directory'], $context['parent_trust']);
    clearstatcache(true, $path);
    $before = @lstat($path);
    if (!is_array($before)) {
        assertDirectoryChain($context['parent_trust']);
        clearstatcache(true, $path);
        if (is_array(@lstat($path))) throw new RuntimeException();
        if (!$createMissing) return ['handle' => null, 'identity' => null,
            'bytes' => null, 'existed' => false];
        $handle = @fopen($path, 'x+b');
        if (!is_resource($handle) || !chmod($path, 0600)) {
            if (is_resource($handle)) fclose($handle);
            throw new RuntimeException();
        }
        $before = fstat($handle);
        if (!is_array($before)) { fclose($handle); throw new RuntimeException(); }
        fsyncScopeJournalFile($handle);
        fsyncScopeJournalDirectory($context['directory_handle']);
        $identity = persistentJournalFileCurrent($path, $handle, $before, 65536);
        // Interposition boundary: retained-missing-target-created.
        persistentJournalLeaseCurrent($context, ['handle' => $handle, 'identity' => $identity]);
        return ['handle' => $handle, 'identity' => $identity, 'bytes' => '', 'existed' => false];
    }
    if (is_link($path) || (($before['mode'] & 0170777) !== 0100600)
        || $before['uid'] !== posix_geteuid() || $before['nlink'] !== 1
        || $before['size'] < 0 || $before['size'] > 65536) throw new RuntimeException();
    $handle = @fopen($path, $writable ? 'r+b' : 'rb');
    if (!is_resource($handle)) throw new RuntimeException();
    try {
        $identity = persistentJournalFileCurrent($path, $handle, $before, 65536);
        $bytes = persistentJournalReadDescriptor($handle, 65536);
        $identity = persistentJournalFileCurrent($path, $handle, $identity, 65536);
        // Interposition boundary: retained-target-opened.
        $identity = persistentJournalFileCurrent($path, $handle, $identity, 65536);
        persistentJournalDirectoryCurrent($context['directory_handle'],
            $context['directory'], $context['parent_trust']);
    } catch (Throwable $error) {
        fclose($handle);
        throw $error;
    }
    return ['handle' => $handle, 'identity' => $identity, 'bytes' => $bytes, 'existed' => true];
}

/** @return array{bytes:string,identity:array} */
function persistentJournalApply(array &$context, array $record): array
{
    $intended = base64_decode($record['intended_body_base64'], true);
    if (!is_string($intended)) throw new RuntimeException();
    $target = persistentJournalOpenTarget($context, true);
    if ($target['identity'] !== null) {
        if ($target['identity']['dev'] !== $record['target_dev']
            || $target['identity']['ino'] !== $record['target_ino']) {
            fclose($target['handle']);
            throw new RuntimeException();
        }
        $currentHash = hash('sha256', $target['bytes']);
        $isIntended = hash_equals($record['intended_sha256'], $currentHash)
            && (!$record['prior_target_exists'] || ($target['identity']['dev'] === $record['prior_target_dev']
                && $target['identity']['ino'] === $record['prior_target_ino']));
        $samePriorInode = $record['prior_target_exists']
            && $target['identity']['dev'] === $record['prior_target_dev']
            && $target['identity']['ino'] === $record['prior_target_ino'];
        $isPrior = $samePriorInode
            && hash_equals($record['prior_target_sha256'], $currentHash);
        $prefixAuthorized = $record['phase'] === 'bootstrap' || $record['generation'] === 2;
        $isPrefix = $prefixAuthorized && (!$record['prior_target_exists'] || $samePriorInode)
            && strlen($target['bytes']) < strlen($intended)
            && str_starts_with($intended, $target['bytes']);
        if (!$isIntended && !$isPrior && !$isPrefix) {
            fclose($target['handle']);
            throw new RuntimeException();
        }
        if (!$isIntended) {
            // Crash-resume boundary: before-target-rewrite.
            persistentJournalLeaseCurrent($context, $target);
            $target['identity'] = rewriteScopeJournalDescriptor($target['handle'], $intended);
            persistentJournalLeaseCurrent($context, $target);
        }
    } else {
        throw new RuntimeException();
    }
    persistentJournalLeaseCurrent($context, $target);
    fsyncScopeJournalDirectory($context['directory_handle']);
    persistentJournalLeaseCurrent($context, $target);
    persistentJournalDirectoryCurrent($context['directory_handle'],
        $context['directory'], $context['parent_trust']);
    $stored = persistentJournalFileCurrent($context['path'], $target['handle'],
        $target['identity'], 65536);
    $readback = persistentJournalReadDescriptor($target['handle'], 65536);
    fclose($target['handle']);
    if (!hash_equals($record['intended_sha256'], hash('sha256', $readback))) {
        throw new RuntimeException();
    }
    return ['bytes' => $readback, 'identity' => $stored];
}

function persistentJournalCompleteRecord(string $journal, array $bootstrap,
    array $bindings): array
{
    if ($bootstrap['generation'] === PHP_INT_MAX) throw new RuntimeException();
    $intended = base64_decode($bootstrap['intended_body_base64'], true);
    if (!is_string($intended)) throw new RuntimeException();
    $priorId = $bootstrap['prior_target_exists'] ? [
        'dev' => $bootstrap['prior_target_dev'], 'ino' => $bootstrap['prior_target_ino'],
    ] : null;
    $priorBytes = null;
    if ($priorId !== null) {
        // The record constructor only needs bytes to reproduce the already-validated hash.
        // Build canonically, then substitute the bound hash before checksumming.
        $priorBytes = '';
    }
    $record = persistentJournalRecord($journal, 1 - $bootstrap['slot'],
        $bootstrap['generation'] + 1, $bindings, $priorId, $priorBytes,
        $intended, 'complete');
    if ($priorId !== null) {
        $record['prior_target_sha256'] = $bootstrap['prior_target_sha256'];
        unset($record['record_sha256']);
        ksort($record, SORT_STRING);
        $record['record_sha256'] = hash('sha256', json_encode(
            $record, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR));
        ksort($record, SORT_STRING);
    }
    return $record;
}

function publishScopeJournalComplete(array &$context, string $journal,
    array $selected, array $bindings): array
{
    if ($selected['record']['phase'] === 'complete') {
        persistJournalCompletionGeneration($context, $selected['record']['generation']);
        return $selected;
    }
    $record = persistentJournalCompleteRecord($journal, $selected['record'], $bindings);
    $slot = $record['slot'];
    repairScopeJournalSlot($context, $slot, persistentJournalEncode($record), false);
    persistentJournalLeaseCurrent($context);
    fsyncScopeJournalDirectory($context['directory_handle']);
    persistentJournalLeaseCurrent($context);
    persistJournalCompletionGeneration($context, $record['generation']);
    return ['active' => $slot, 'record' => $record,
        'records' => [$selected['record']['slot'] => $selected['record'], $slot => $record]];
}

function adoptScopeJournalCompletionRemnant(array &$context, string $journal,
    array $slotBytes, array $bindings): ?array
{
    try {
        $bootstrap = persistentJournalDecode($slotBytes[1], $journal, 1, $bindings);
    } catch (Throwable) {
        return null;
    }
    if ($bootstrap['generation'] !== 1 || $bootstrap['phase'] !== 'bootstrap') return null;
    $completed = persistentJournalCompletionGeneration($context);
    if ($completed !== null && $completed >= 2) return null;
    $complete = persistentJournalCompleteRecord($journal, $bootstrap, $bindings);
    $canonical = persistentJournalEncode($complete);
    if ($slotBytes[0] !== '' && $slotBytes[0] !== $canonical
        && !(strlen($slotBytes[0]) < strlen($canonical)
            && str_starts_with($canonical, $slotBytes[0]))) return null;
    repairScopeJournalSlot($context, 0, $canonical, true);
    return selectScopeJournalGeneration([
        persistentJournalReadDescriptor($context['slots'][0]['handle'], 131072),
        $slotBytes[1],
    ], $journal, $bindings);
}

/** @return ?array */
function persistentJournalRecover(array &$context, string $journal): ?array
{
    $slotBytes = [persistentJournalReadDescriptor($context['slots'][0]['handle'], 131072),
        persistentJournalReadDescriptor($context['slots'][1]['handle'], 131072)];
    if ($slotBytes[0] === '' && $slotBytes[1] === '') return null;
    $target = persistentJournalOpenTarget($context, false);
    if (!is_resource($target['handle'])) throw new RuntimeException();
    $bindings = persistentJournalBindings($context, $target['identity']);
    fclose($target['handle']);
    try {
        $selected = selectScopeJournalGeneration($slotBytes, $journal, $bindings);
    } catch (Throwable $error) {
        $selected = adoptScopeJournalBootstrapRemnant($context, $journal, $slotBytes, $bindings)
            ?? adoptScopeJournalCompletionRemnant($context, $journal, $slotBytes, $bindings);
        if ($selected === null) throw $error;
    }
    $completed = persistentJournalCompletionGeneration($context,
        $selected['record']['phase'] === 'complete'
            ? $selected['record']['generation'] : null);
    if ($completed !== null && $completed > $selected['record']['generation']) {
        throw new RuntimeException();
    }
    persistentJournalApply($context, $selected['record']);
    return publishScopeJournalComplete($context, $journal, $selected, $bindings);
}

function readScopeJournalTargetInContext(array &$context): ?string
{
    $target = persistentJournalOpenTarget($context, false);
    $bytes = $target['bytes'];
    if (is_resource($target['handle'])) fclose($target['handle']);
    return $bytes === '' ? null : $bytes;
}

function readScopeJournalInContext(array &$context, string $journal): ?string
{
    $selected = persistentJournalRecover($context, $journal);
    if ($selected !== null) return base64_decode(
        $selected['record']['intended_body_base64'], true
    );
    return readScopeJournalTargetInContext($context);
}

function readScopeJournal(string $home, string $journal): ?string
{
    $context = persistentJournalOpenContext($home, $journal);
    try {
        return readScopeJournalInContext($context, $journal);
    } finally {
        persistentJournalCloseContext($context);
    }
}

function writeScopeJournalInContext(array &$context, string $journal, string $bytes,
    ?array $expectedTarget = null): string
{
    if ($bytes === '' || strlen($bytes) > 65536) throw new RuntimeException();
    $slotBytes = [persistentJournalReadDescriptor($context['slots'][0]['handle'], 131072),
        persistentJournalReadDescriptor($context['slots'][1]['handle'], 131072)];
    if ($slotBytes[0] === '' && $slotBytes[1] === '') {
        $target = $expectedTarget ?? persistentJournalOpenTarget($context, true, true);
        $closeTarget = $expectedTarget === null;
        if (!is_resource($target['handle'])) throw new RuntimeException();
        persistentJournalLeaseCurrent($context, $target);
        $bindings = persistentJournalBindings($context, $target['identity']);
        $priorExists = $target['existed'] && $target['bytes'] !== '';
        $selected = initializeScopeJournalBootstrapRemnant($context, $journal,
            $bindings, $priorExists ? $target['identity'] : null,
            $priorExists ? $target['bytes'] : null, $bytes);
        if ($closeTarget) fclose($target['handle']);
        persistentJournalApply($context, $selected['record']);
        $selected = publishScopeJournalComplete($context, $journal, $selected, $bindings);
    } else {
        $selected = null;
        if ($slotBytes[1] === '') {
            $target = $expectedTarget ?? persistentJournalOpenTarget($context, true);
            $closeTarget = $expectedTarget === null;
            if (!is_resource($target['handle'])) throw new RuntimeException();
            persistentJournalLeaseCurrent($context, $target);
            $bindings = persistentJournalBindings($context, $target['identity']);
            try {
                $selected = initializeScopeJournalBootstrapRemnant($context, $journal,
                    $bindings, $target['bytes'] === '' ? null : $target['identity'],
                    $target['bytes'] === '' ? null : $target['bytes'], $bytes);
            } finally {
                if ($closeTarget) fclose($target['handle']);
            }
            persistentJournalApply($context, $selected['record']);
            $selected = publishScopeJournalComplete($context, $journal, $selected, $bindings);
        }
        if ($selected === null) $selected = persistentJournalRecover($context, $journal);
        if ($selected === null || $selected['record']['phase'] !== 'complete') {
            throw new RuntimeException();
        }
        $target = persistentJournalOpenTarget($context, true);
        if (!is_resource($target['handle'])
            || $selected['record']['generation'] > PHP_INT_MAX - 2) {
            if (is_resource($target['handle'])) fclose($target['handle']);
            throw new RuntimeException();
        }
        $bindings = persistentJournalBindings($context, $target['identity']);
        $inactive = 1 - $selected['active'];
        $record = persistentJournalRecord($journal, $inactive,
            $selected['record']['generation'] + 1, $bindings,
            $target['identity'], $target['bytes'], $bytes, 'bootstrap');
        fclose($target['handle']);
        repairScopeJournalSlot($context, $inactive, persistentJournalEncode($record), false);
        fsyncScopeJournalDirectory($context['directory_handle']);
        persistentJournalLeaseCurrent($context);
        $selected = ['active' => $inactive, 'record' => $record];
        persistentJournalApply($context, $selected['record']);
        $selected = publishScopeJournalComplete($context, $journal, $selected, $bindings);
    }
    $stored = persistentJournalApply($context, $selected['record']);
    return hash('sha256', $stored['bytes']);
}

function writeScopeJournal(string $home, string $journal, string $bytes): string
{
    $context = persistentJournalOpenContext($home, $journal);
    try {
        return writeScopeJournalInContext($context, $journal, $bytes);
    } finally {
        persistentJournalCloseContext($context);
    }
}

function compareAndSwapScopeJournal(string $home, string $journal,
    ?string $expected, string $desired): string
{
    if ($journal !== 'target-sync' || $desired === '' || strlen($desired) > 65536
        || ($expected !== null && ($expected === '' || strlen($expected) > 65536))) {
        throw new RuntimeException();
    }
    $context = persistentJournalOpenContext($home, $journal);
    $retainedTarget = null;
    try {
        $retainedTarget = persistentJournalOpenTarget($context, false);
        try {
            $current = readScopeJournalInContext($context, $journal);
        } catch (Throwable $readError) {
            $target = $retainedTarget['bytes'] === '' ? null : $retainedTarget['bytes'];
            if (($expected === null) !== ($target === null)
                || ($expected !== null && !hash_equals($expected, (string) $target))) {
                throw $readError;
            }
            if (!is_resource($retainedTarget['handle'])) throw $readError;
            persistentJournalLeaseCurrent($context, $retainedTarget);
            // Before any durable generation exists, only the exact desired
            // bootstrap prefix plus the exact retained expected target can
            // authorize repair of an interrupted create or first replacement.
            writeScopeJournalInContext($context, $journal, $desired, $retainedTarget);
            return 'changed';
        }
        if ($current !== null && hash_equals($desired, $current)) return 'already_applied';
        if (($expected === null) !== ($current === null)
            || ($expected !== null && !hash_equals($expected, (string) $current))) {
            return 'conflict';
        }
        if (is_resource($retainedTarget['handle'])) {
            persistentJournalLeaseCurrent($context, $retainedTarget);
        }
        writeScopeJournalInContext($context, $journal, $desired,
            is_resource($retainedTarget['handle']) ? $retainedTarget : null);
        return 'changed';
    } finally {
        if (is_resource($retainedTarget['handle'] ?? null)) fclose($retainedTarget['handle']);
        persistentJournalCloseContext($context);
    }
}

function validateConfigSchema(array $config): void
{
    $url = $config['webhook_url'] ?? null;
    $recipients = $config['error_recipients'] ?? null;
    $logPath = $config['log_path'] ?? null;
    $pinned = $config['notification_pinned_targets'] ?? null;
    $targets = $config['notification_targets'] ?? null;
    $hmacEncoded = $config['system_mail_hmac_key'] ?? null;
    $webhookPattern = '@\Ahttps://webhook[.]worksmobile[.]com/message/'
        . '[A-Za-z0-9._~-]+\z@D';
    if (!is_string($url)
        || preg_match($webhookPattern, $url) !== 1
        || !is_array($recipients) || $recipients === []
        || !is_string($logPath) || $logPath === '' || !str_starts_with($logPath, '/')) {
        throw new RuntimeException();
    }
    try {
        $canonicalRecipients = canonicalEmailList($recipients, false);
        $canonicalPinned = canonicalEmailList($pinned, true);
        $canonicalTargets = canonicalEmailList($targets, true);
    } catch (Throwable) {
        throw new RuntimeException();
    }
    if ($recipients !== $canonicalRecipients || $pinned !== $canonicalPinned
        || $targets !== $canonicalTargets || !is_string($hmacEncoded)
        || preg_match('/\A[A-Za-z0-9_-]{43}\z/D', $hmacEncoded) !== 1) {
        throw new RuntimeException();
    }
    $sizes = runtimeConfigSizes(
        $canonicalRecipients, $canonicalPinned, $canonicalTargets, $logPath
    );
    if ($sizes['recipient_count'] > MAX_RECIPIENTS || $sizes['to_bytes'] > MAX_TO_BYTES
        || $sizes['log_path_bytes'] > MAX_LOG_PATH_BYTES
        || $sizes['header_line_bytes'] > MAX_HEADER_LINE_BYTES
        || $sizes['signed_message_bytes'] > MAX_SIGNED_MESSAGE_BYTES) {
        throw new RuntimeException();
    }
    $hmacKey = base64_decode(strtr($hmacEncoded, '-_', '+/') . '=', true);
    if (!is_string($hmacKey) || strlen($hmacKey) !== 32
        || rtrim(strtr(base64_encode($hmacKey), '+/', '-_'), '=') !== $hmacEncoded) {
        throw new RuntimeException();
    }
    $components = preg_split('~/+~', strtolower(str_replace('\\', '/', $logPath)),
        -1, PREG_SPLIT_NO_EMPTY) ?: [];
    if (in_array('public_html', $components, true)) {
        throw new RuntimeException();
    }
}

/** @return resource */
function acquireTransactionLock(string $directory)
{
    $path = $directory . '/.manage-private-config.lock';
    $oldUmask = umask(0077);
    try {
        $handle = @fopen($path, 'x+b');
    } finally {
        umask($oldUmask);
    }
    if (is_resource($handle)) {
        if (!chmod($path, 0600)) {
            fclose($handle);
            @unlink($path);
            throw new RuntimeException();
        }
    } else {
        clearstatcache(true, $path);
        $before = @lstat($path);
        if (!is_array($before) || is_link($path) || (($before['mode'] & 0170000) !== 0100000)
            || $before['uid'] !== posix_geteuid() || (($before['mode'] & 0777) !== 0600)) {
            throw new RuntimeException();
        }
        $handle = @fopen($path, 'r+b');
        if (!is_resource($handle)) {
            throw new RuntimeException();
        }
        $opened = fstat($handle);
        if (!is_array($opened) || $opened['dev'] !== $before['dev'] || $opened['ino'] !== $before['ino']
            || $opened['uid'] !== posix_geteuid() || (($opened['mode'] & 0170000) !== 0100000)
            || (($opened['mode'] & 0777) !== 0600)) {
            fclose($handle);
            throw new RuntimeException();
        }
    }
    if (!flock($handle, LOCK_EX)) {
        fclose($handle);
        throw new RuntimeException();
    }
    return $handle;
}

/** @return array{0: array<string, mixed>, 1: string, 2: string} */
function readConfig(string $path): array
{
    clearstatcache(true, $path);
    $before = @lstat($path);
    if (!is_array($before) || is_link($path) || (($before['mode'] & 0170000) !== 0100000)
        || $before['uid'] !== posix_geteuid() || (($before['mode'] & 0777) !== 0600)
        || $before['size'] < 1 || $before['size'] > CONFIG_LIMIT) {
        throw new RuntimeException();
    }
    $handle = @fopen($path, 'rb');
    if (!is_resource($handle)) {
        throw new RuntimeException();
    }
    try {
        $opened = fstat($handle);
        if (!is_array($opened) || $opened['dev'] !== $before['dev'] || $opened['ino'] !== $before['ino']
            || $opened['uid'] !== posix_geteuid() || (($opened['mode'] & 0170000) !== 0100000)
            || (($opened['mode'] & 0777) !== 0600) || $opened['size'] !== $before['size']) {
            throw new RuntimeException();
        }
        $bytes = stream_get_contents($handle, CONFIG_LIMIT + 1);
        if (!is_string($bytes) || strlen($bytes) !== $opened['size'] || strlen($bytes) > CONFIG_LIMIT) {
            throw new RuntimeException();
        }
    } finally {
        fclose($handle);
    }
    $config = json_decode($bytes, true, 64, JSON_THROW_ON_ERROR);
    if (!is_array($config) || array_is_list($config)) {
        throw new RuntimeException();
    }
    validateConfigSchema($config);
    return [$config, $bytes, hash('sha256', $bytes)];
}

function configBytes(array $config): string
{
    if (array_is_list($config)) {
        throw new RuntimeException();
    }
    validateConfigSchema($config);
    $bytes = json_encode($config, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR) . "\n";
    if (strlen($bytes) > CONFIG_LIMIT) {
        throw new RuntimeException();
    }
    return $bytes;
}

function atomicReplace(string $path, string $bytes, string $expectedCurrentHash,
    array $directoryTrust): void
{
    $directory = dirname($path);
    assertDirectoryChain($directoryTrust);
    $temporary = $directory . '/.config-' . bin2hex(random_bytes(16)) . '.tmp';
    $handle = @fopen($temporary, 'x+b');
    if (!is_resource($handle)) {
        throw new RuntimeException();
    }
    try {
        if (!chmod($temporary, 0600)) {
            throw new RuntimeException();
        }
        $offset = 0;
        while ($offset < strlen($bytes)) {
            $written = fwrite($handle, substr($bytes, $offset));
            if (!is_int($written) || $written < 1) {
                throw new RuntimeException();
            }
            $offset += $written;
        }
        if (!fflush($handle)) {
            throw new RuntimeException();
        }
        $info = fstat($handle);
        if (!is_array($info) || (($info['mode'] & 0170000) !== 0100000)
            || (($info['mode'] & 0777) !== 0600) || $info['uid'] !== posix_geteuid()
            || $info['size'] !== strlen($bytes)) {
            throw new RuntimeException();
        }
        rewind($handle);
        $stored = stream_get_contents($handle, CONFIG_LIMIT + 1);
        if (!is_string($stored) || !hash_equals(hash('sha256', $bytes), hash('sha256', $stored))) {
            throw new RuntimeException();
        }
    } catch (Throwable $error) {
        fclose($handle);
        @unlink($temporary);
        throw $error;
    }
    fclose($handle);
    try {
        [, , $currentHash] = readConfig($path);
    } catch (Throwable $error) {
        @unlink($temporary);
        throw $error;
    }
    if (!hash_equals($expectedCurrentHash, $currentHash)) {
        @unlink($temporary);
        throw new ConfigConflict();
    }
    try {
        assertDirectoryChain($directoryTrust);
    } catch (Throwable $error) {
        @unlink($temporary);
        throw $error;
    }
    if (!@rename($temporary, $path)) {
        @unlink($temporary);
        throw new RuntimeException();
    }
}

function emitResponse(array $response): never
{
    $bytes = json_encode($response, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
    if (strlen($bytes) > REQUEST_LIMIT) {
        throw new RuntimeException();
    }
    fwrite(STDOUT, $bytes);
    exit(0);
}

function assertNoDecodedDuplicateJsonKeys(string $bytes): void
{
    $length = strlen($bytes);
    $offset = 0;
    $skipWhitespace = static function () use ($bytes, $length, &$offset): void {
        while ($offset < $length && str_contains(" \t\r\n", $bytes[$offset])) {
            $offset++;
        }
    };
    $readString = static function () use ($bytes, $length, &$offset): string {
        if ($offset >= $length || $bytes[$offset] !== '"') {
            throw new RuntimeException();
        }
        $start = $offset++;
        while ($offset < $length) {
            if ($bytes[$offset] === '\\') {
                $offset += 2;
                continue;
            }
            if ($bytes[$offset++] === '"') {
                $encoded = substr($bytes, $start, $offset - $start);
                $decoded = json_decode($encoded, false, 2, JSON_THROW_ON_ERROR);
                if (!is_string($decoded)) {
                    throw new RuntimeException();
                }
                return $decoded;
            }
        }
        throw new RuntimeException();
    };
    $skipWhitespace();
    if ($offset >= $length || $bytes[$offset++] !== '{') {
        throw new RuntimeException();
    }
    $decodedKeys = [];
    while (true) {
        $skipWhitespace();
        if ($offset < $length && $bytes[$offset] === '}') {
            $offset++;
            break;
        }
        $decodedKey = $readString();
        if (array_key_exists('key:' . $decodedKey, $decodedKeys)) {
            throw new RuntimeException();
        }
        $decodedKeys['key:' . $decodedKey] = true;
        $skipWhitespace();
        if ($offset >= $length || $bytes[$offset++] !== ':') {
            throw new RuntimeException();
        }
        $skipWhitespace();
        $depth = 0;
        $inString = false;
        $escaped = false;
        while ($offset < $length) {
            $character = $bytes[$offset];
            if ($inString) {
                $offset++;
                if ($escaped) {
                    $escaped = false;
                } elseif ($character === '\\') {
                    $escaped = true;
                } elseif ($character === '"') {
                    $inString = false;
                }
                continue;
            }
            if ($character === '"') {
                $inString = true;
                $offset++;
            } elseif ($character === '{' || $character === '[') {
                $depth++;
                $offset++;
            } elseif ($character === '}' || $character === ']') {
                if ($depth === 0) {
                    break;
                }
                $depth--;
                $offset++;
            } elseif ($character === ',' && $depth === 0) {
                break;
            } else {
                $offset++;
            }
        }
        $skipWhitespace();
        if ($offset < $length && $bytes[$offset] === ',') {
            $offset++;
            continue;
        }
        if ($offset < $length && $bytes[$offset] === '}') {
            $offset++;
            break;
        }
        throw new RuntimeException();
    }
    $skipWhitespace();
    if ($offset !== $length) {
        throw new RuntimeException();
    }
}

function healthSummary(array $config, string $home): array
{
    $path = dirname($config['log_path']) . '/delivery-health.json';
    if (strlen($path) > 4096 || str_contains(strtolower($path), '/public_html/')
        || !str_starts_with($path, $home . '/')) {
        throw new RuntimeException();
    }
    $directory = dirname($path);
    $parentTrust = [
        $home => ['snapshot' => directorySnapshot($home, null), 'mode' => null],
    ];
    $relative = substr($directory, strlen($home) + 1);
    $cursor = $home;
    foreach (explode('/', $relative) as $component) {
        if ($component === '' || $component === '.' || $component === '..') {
            throw new RuntimeException();
        }
        $cursor .= '/' . $component;
        $parentTrust[$cursor] = ['snapshot' => directorySnapshot($cursor), 'mode' => 0700];
    }
    clearstatcache(true, $path);
    $before = @lstat($path);
    if (!is_array($before)) {
        assertDirectoryChain($parentTrust);
        clearstatcache(true, $path);
        if (is_array(@lstat($path))) {
            throw new RuntimeException();
        }
        assertDirectoryChain($parentTrust);
        return ['schema_version' => 1, 'state' => 'missing', 'changed_at' => null,
            'classification' => null, 'next_observation_sequence' => 0,
            'last_applied_sequence' => 0];
    }
    if (is_link($path) || (($before['mode'] & 0170000) !== 0100000)
        || $before['uid'] !== posix_geteuid() || (($before['mode'] & 0777) !== 0600)
        || $before['nlink'] !== 1 || $before['size'] < 1 || $before['size'] > 4096) {
        throw new RuntimeException();
    }
    $handle = @fopen($path, 'rb');
    if (!is_resource($handle)) {
        throw new RuntimeException();
    }
    $opened = fstat($handle);
    $bytes = stream_get_contents($handle, 4097);
    $openedAfterRead = fstat($handle);
    fclose($handle);
    if (!is_array($opened) || !is_array($openedAfterRead)
        || (($opened['mode'] & 0170000) !== 0100000) || (($opened['mode'] & 0777) !== 0600)
        || $opened['dev'] !== $before['dev'] || $opened['ino'] !== $before['ino']
        || $opened['uid'] !== posix_geteuid() || $opened['nlink'] !== 1
        || $opened['size'] !== $before['size']
        || $openedAfterRead['dev'] !== $opened['dev']
        || $openedAfterRead['ino'] !== $opened['ino']
        || $openedAfterRead['uid'] !== $opened['uid']
        || $openedAfterRead['mode'] !== $opened['mode']
        || $openedAfterRead['nlink'] !== $opened['nlink']
        || $openedAfterRead['size'] !== $opened['size']
        || !is_string($bytes) || strlen($bytes) > 4096
        || strlen($bytes) !== $opened['size']) {
        throw new RuntimeException();
    }
    assertDirectoryChain($parentTrust);
    clearstatcache(true, $path);
    $after = @lstat($path);
    if (!is_array($after) || is_link($path) || (($after['mode'] & 0170000) !== 0100000)
        || (($after['mode'] & 0777) !== 0600) || $after['uid'] !== $before['uid']
        || $after['nlink'] !== 1 || $after['dev'] !== $before['dev']
        || $after['ino'] !== $before['ino'] || $after['size'] !== $before['size']) {
        throw new RuntimeException();
    }
    $health = json_decode($bytes, true, 16, JSON_THROW_ON_ERROR);
    if (!is_array($health) || array_is_list($health)) {
        throw new RuntimeException();
    }
    assertNoDecodedDuplicateJsonKeys($bytes);
    $keys = array_keys($health);
    sort($keys, SORT_STRING);
    $status = $health['status'] ?? null;
    $expectedKeys = $status === 'degraded'
        ? ['changed_at', 'classification', 'last_applied_sequence', 'message_id_hash',
            'next_observation_sequence', 'schema_version', 'status']
        : ['changed_at', 'last_applied_sequence', 'next_observation_sequence',
            'schema_version', 'status'];
    $classifications = ['success', 'invalid_payload', 'invalid_parameter',
        'missing_parameter', 'invalid_webhook_url', 'rate_limited', 'http_error',
        'transport_error', 'forced_test_failure', 'internal_error',
        'system_mail_suppressed', 'health_state_failure', 'unknown'];
    $changedAt = $health['changed_at'] ?? null;
    $classification = $status === 'healthy' ? 'success' : ($health['classification'] ?? null);
    $next = $health['next_observation_sequence'] ?? null;
    $applied = $health['last_applied_sequence'] ?? null;
    if ($keys !== $expectedKeys || ($health['schema_version'] ?? null) !== 1
        || !in_array($status, ['healthy', 'degraded'], true)
        || !is_string($changedAt)
        || preg_match('/\A[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\z/D', $changedAt) !== 1
        || !is_string($classification) || !in_array($classification, $classifications, true)
        || !is_int($next) || $next < 0 || !is_int($applied) || $applied < 0 || $applied > $next
        || ($status === 'degraded' && (!is_string($health['message_id_hash'] ?? null)
            || preg_match('/\A[a-f0-9]{64}\z/D', $health['message_id_hash']) !== 1
            || $classification === 'success'))) {
        throw new RuntimeException();
    }
    $parsed = DateTimeImmutable::createFromFormat('!Y-m-d\TH:i:s\Z', $changedAt,
        new DateTimeZone('UTC'));
    if (!$parsed instanceof DateTimeImmutable || $parsed->format('Y-m-d\TH:i:s\Z') !== $changedAt) {
        throw new RuntimeException();
    }
    return ['schema_version' => 1, 'state' => $status, 'changed_at' => $changedAt,
        'classification' => $classification, 'next_observation_sequence' => $next,
        'last_applied_sequence' => $applied];
}

try {
    if ($argc !== 1) {
        throw new RuntimeException();
    }
    $home = dirname(__DIR__, 3);
    if (preg_match('/\A\/home\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z/D', $home) !== 1) {
        throw new RuntimeException();
    }
    $path = $home . '/mail-lineworks/private/config.json';
    $configDirectory = dirname($path);
    foreach (explode('/', trim($path, '/')) as $component) {
        if (strtolower($component) === 'public_html') {
            throw new RuntimeException();
        }
    }
    $directoryTrust = [
        $home => ['snapshot' => directorySnapshot($home, null), 'mode' => null],
        $home . '/mail-lineworks' => [
            'snapshot' => directorySnapshot($home . '/mail-lineworks'), 'mode' => 0700],
        $configDirectory => ['snapshot' => directorySnapshot($configDirectory), 'mode' => 0700],
    ];
    $input = stream_get_contents(STDIN, REQUEST_LIMIT + 1);
    if (!is_string($input) || strlen($input) > REQUEST_LIMIT) {
        throw new RuntimeException();
    }
    $request = json_decode($input, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($request) || array_is_list($request)) {
        throw new RuntimeException();
    }
    $keys = array_keys($request);
    sort($keys, SORT_STRING);
    if (($request['schema_version'] ?? null) !== 1 || !is_string($request['operation'] ?? null)) {
        throw new RuntimeException();
    }
    assertDirectoryChain($directoryTrust);
    $transactionLock = acquireTransactionLock($configDirectory);
    assertDirectoryChain($directoryTrust);
    [$oldConfig, $oldBytes, $oldHash] = readConfig($path);
    if ($request['operation'] === 'read' && $keys === ['operation', 'schema_version']) {
        emitResponse(['schema_version' => 1, 'config' => $oldConfig, 'sha256' => $oldHash]);
    }
    if ($request['operation'] === 'health-summary' && $keys === ['operation', 'schema_version']) {
        emitResponse(healthSummary($oldConfig, $home));
    }
    if ($request['operation'] === 'scope-journal-read'
        && $keys === ['journal', 'operation', 'schema_version']
        && is_string($request['journal'])) {
        $journalBytes = readScopeJournal($home, $request['journal']);
        emitResponse($journalBytes === null
            ? ['schema_version' => 1, 'state' => 'missing']
            : ['schema_version' => 1, 'state' => 'present',
                'body_base64' => base64_encode($journalBytes)]);
    }
    if ($request['operation'] === 'scope-journal-compare-and-swap'
        && $keys === ['desired', 'expected', 'journal', 'operation', 'schema_version']
        && $request['journal'] === 'target-sync'
        && is_array($request['expected']) && !array_is_list($request['expected'])
        && is_array($request['desired']) && !array_is_list($request['desired'])) {
        $expectedKeys = array_keys($request['expected']);
        $desiredKeys = array_keys($request['desired']);
        sort($expectedKeys, SORT_STRING);
        sort($desiredKeys, SORT_STRING);
        $expected = null;
        if (($request['expected']['state'] ?? null) === 'missing'
            && $expectedKeys === ['state']) {
            $expected = null;
        } elseif (($request['expected']['state'] ?? null) === 'present'
            && $expectedKeys === ['body_base64', 'sha256', 'state']
            && is_string($request['expected']['body_base64'])
            && is_string($request['expected']['sha256'])) {
            $expected = base64_decode($request['expected']['body_base64'], true);
            if (!is_string($expected) || $expected === '' || strlen($expected) > 65536
                || base64_encode($expected) !== $request['expected']['body_base64']
                || preg_match('/\A[a-f0-9]{64}\z/D', $request['expected']['sha256']) !== 1
                || !hash_equals(hash('sha256', $expected), $request['expected']['sha256'])) {
                throw new RuntimeException();
            }
        } else {
            throw new RuntimeException();
        }
        if ($desiredKeys !== ['body_base64', 'sha256']
            || !is_string($request['desired']['body_base64'] ?? null)
            || !is_string($request['desired']['sha256'] ?? null)) {
            throw new RuntimeException();
        }
        $desired = base64_decode($request['desired']['body_base64'], true);
        if (!is_string($desired) || $desired === '' || strlen($desired) > 65536
            || base64_encode($desired) !== $request['desired']['body_base64']
            || preg_match('/\A[a-f0-9]{64}\z/D', $request['desired']['sha256']) !== 1
            || !hash_equals(hash('sha256', $desired), $request['desired']['sha256'])) {
            throw new RuntimeException();
        }
        emitResponse(['schema_version' => 1, 'status' => compareAndSwapScopeJournal(
            $home, 'target-sync', $expected, $desired
        )]);
    }
    if ($request['operation'] === 'scope-journal-write'
        && $keys === ['body_base64', 'journal', 'operation', 'schema_version']
        && is_string($request['body_base64']) && $request['journal'] === 'filter') {
        $journal = base64_decode($request['body_base64'], true);
        if (!is_string($journal) || base64_encode($journal) !== $request['body_base64']) {
            throw new RuntimeException();
        }
        emitResponse(['schema_version' => 1, 'sha256' => writeScopeJournal(
            $home, $request['journal'], $journal
        )]);
    }
    if ($request['operation'] !== 'compare_and_swap'
        || $keys !== ['config', 'expected_sha256', 'operation', 'schema_version']
        || !is_string($request['expected_sha256'])
        || preg_match('/\A[a-f0-9]{64}\z/D', $request['expected_sha256']) !== 1
        || !is_array($request['config']) || array_is_list($request['config'])) {
        throw new RuntimeException();
    }
    if (!hash_equals($oldHash, $request['expected_sha256'])) {
        emitResponse(['schema_version' => 1, 'status' => 'conflict',
            'old_sha256' => $oldHash, 'new_sha256' => $oldHash]);
    }
    foreach (array_keys($oldConfig) as $key) {
        if (!array_key_exists($key, $request['config'])) {
            throw new RuntimeException();
        }
    }
    if ($request['config'] === $oldConfig) {
        emitResponse(['schema_version' => 1, 'status' => 'unchanged',
            'old_sha256' => $oldHash, 'new_sha256' => $oldHash]);
    }
    $newBytes = configBytes($request['config']);
    $intendedHash = hash('sha256', $newBytes);
    try {
        atomicReplace($path, $newBytes, $oldHash, $directoryTrust);
    } catch (ConfigConflict $conflict) {
        [, , $currentHash] = readConfig($path);
        emitResponse(['schema_version' => 1, 'status' => 'conflict',
            'old_sha256' => $oldHash, 'new_sha256' => $currentHash]);
    }
    try {
        [, , $storedHash] = readConfig($path);
    } catch (Throwable $readbackError) {
        clearstatcache(true, $path);
        $current = @file_get_contents($path, false, null, 0, CONFIG_LIMIT + 1);
        if (is_string($current) && hash_equals($intendedHash, hash('sha256', $current))) {
            atomicReplace($path, $oldBytes, $intendedHash, $directoryTrust);
            [, , $restoredHash] = readConfig($path);
            emitResponse(['schema_version' => 1, 'status' => 'restored',
                'old_sha256' => $oldHash, 'new_sha256' => $restoredHash]);
        }
        throw $readbackError;
    }
    if (!hash_equals($intendedHash, $storedHash)) {
        emitResponse(['schema_version' => 1, 'status' => 'conflict',
            'old_sha256' => $oldHash, 'new_sha256' => $storedHash]);
    }
    emitResponse(['schema_version' => 1, 'status' => 'changed',
        'old_sha256' => $oldHash, 'new_sha256' => $storedHash]);
} catch (Throwable $error) {
    failClosed();
}
