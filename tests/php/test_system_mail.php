<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';

use XserverMail\SystemMailAuthenticator;

function systemCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function systemRejects(SystemMailAuthenticator $auth, string $raw, string $message): void
{
    systemCheck(!$auth->isAuthentic($raw), $message);
}

function canonicalBody(string $body): string
{
    return str_replace("\r", "\n", str_replace("\r\n", "\n", $body));
}

/** @param list<string> $fields */
function framedHmac(string $key, array $fields): string
{
    $framed = '';
    foreach ($fields as $field) {
        $framed .= strlen($field) . ':' . $field . "\n";
    }
    return hash_hmac('sha256', $framed, $key, false);
}

/** @param list<string> $extraHeaders */
function syntheticSystemMail(
    string $key,
    string $body,
    array $extraHeaders = [],
    string $type = 'error',
    string $event = '0123456789abcdef0123456789abcdef',
    string $to = 'operator@example.invalid',
    string $date = 'Mon, 13 Jul 2026 12:00:00 +0000',
    ?string $subject = null,
    string $lineEnd = "\r\n",
): string {
    $subject ??= $type === 'error' ? 'Xserver mail notifier error' : 'Xserver mail notifier recovered';
    $normalized = canonicalBody($body);
    $bodyHash = hash('sha256', $normalized, false);
    $auth = framedHmac($key, ['1', $type, $event, $to, $subject, $date, $bodyHash]);
    $headers = array_merge($extraHeaders, [
        'To: ' . $to,
        'Subject: ' . $subject,
        'Date: ' . $date,
        'X-Xserver-Mail-Notifier-Version: 1',
        'X-Xserver-Mail-Notifier-Type: ' . $type,
        'X-Xserver-Mail-Notifier-Event: ' . $event,
        'X-Xserver-Mail-Notifier-Body-SHA256: ' . $bodyHash,
        'X-Xserver-Mail-Notifier-Auth: hmac-sha256=' . $auth,
    ]);
    return implode($lineEnd, $headers) . $lineEnd . $lineEnd . $body;
}

function recipientsOfLength(int $length): string
{
    for ($count = 1; $count <= 64; ++$count) {
        $localTotal = $length - (16 * $count) - ($count - 1);
        if ($localTotal < 3 * $count || $localTotal > 64 * $count) {
            continue;
        }
        $locals = array_fill(0, $count, 3);
        for ($index = 0; $index < $count && $localTotal > array_sum($locals); ++$index) {
            $locals[$index] += min(61, $localTotal - array_sum($locals));
        }
        $addresses = [];
        foreach ($locals as $index => $localLength) {
            $prefix = sprintf('x%02d', $index);
            $addresses[] = $prefix . str_repeat('a', $localLength - strlen($prefix)) . '@example.invalid';
        }
        sort($addresses, SORT_STRING);
        $value = implode(',', $addresses);
        if (strlen($value) === $length) {
            return $value;
        }
    }
    throw new RuntimeException('Unable to construct recipient boundary');
}

/** @return list<string> */
function paddingHeadersForPrefix(string $base, int $targetPrefix): array
{
    $needed = $targetPrefix - strlen($base);
    systemCheck($needed >= 0, 'Prefix target must fit base headers');
    $headers = [];
    $index = 0;
    while ($needed > 0) {
        // Each header contributes its raw line plus CRLF. Avoid an unusable 1-byte tail.
        $contribution = min(999, $needed);
        if ($needed - $contribution === 1) {
            --$contribution;
        }
        systemCheck($contribution >= 10, 'Padding tail must fit a header');
        $prefix = sprintf('X-P%04d: ', $index++);
        $headers[] = $prefix . str_repeat('p', $contribution - 2 - strlen($prefix));
        $needed -= $contribution;
    }
    return $headers;
}

$keyBytes = "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    . "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f";
$unpaddedKey = rtrim(strtr(base64_encode($keyBytes), '+/', '-_'), '=');
systemCheck(strlen($unpaddedKey) === 43 && !str_contains($unpaddedKey, '='),
    'Deterministic 32-byte key vector must use unpadded base64url config form');
$key = base64_decode(strtr($unpaddedKey, '-_', '+/') . '=', true);
systemCheck(is_string($key) && $key === $keyBytes, 'Unpadded config key vector must decode without byte changes');
$auth = new SystemMailAuthenticator($key);
$date = 'Mon, 13 Jul 2026 12:00:00 +0000';
$event = str_repeat('a', 32);
$wire = $auth->build('error', ['operator@example.invalid'], $date, $event, "Failure\n");
$bodyHash = hash('sha256', "Failure\n", false);
$expectedHmac = framedHmac($key, [
    '1', 'error', $event, 'operator@example.invalid',
    'Xserver mail notifier error', $date, $bodyHash,
]);
$expected = "To: operator@example.invalid\r\n"
    . "Subject: Xserver mail notifier error\r\n"
    . "Date: {$date}\r\n"
    . "X-Xserver-Mail-Notifier-Version: 1\r\n"
    . "X-Xserver-Mail-Notifier-Type: error\r\n"
    . "X-Xserver-Mail-Notifier-Event: {$event}\r\n"
    . "X-Xserver-Mail-Notifier-Body-SHA256: {$bodyHash}\r\n"
    . "X-Xserver-Mail-Notifier-Auth: hmac-sha256={$expectedHmac}\r\n\r\n"
    . "Failure\r\n";
systemCheck($wire === $expected, 'Wire headers, order, framing, lowercase hashes, and body bytes must be exact');
systemCheck($auth->isAuthentic($wire), 'Generated wire must verify');
$mixedBody = $auth->build('error', ['operator@example.invalid'], $date, $event, "A\r\nB\rC\n\n");
systemCheck(str_ends_with($mixedBody, "\r\n\r\nA\r\nB\r\nC\r\n\r\n"),
    'Generated body must normalize line endings and preserve multiple terminal LFs');
systemCheck($auth->isAuthentic($mixedBody), 'Normalized generated body must verify');

foreach (["No terminal LF", "One terminal LF\n", "Two terminal LFs\n\n"] as $terminalBody) {
    $terminalWire = $auth->build('error', ['operator@example.invalid'], $date, $event, $terminalBody);
    $wireBody = substr($terminalWire, strpos($terminalWire, "\r\n\r\n") + 4);
    $expectedWireBody = str_replace("\n", "\r\n", canonicalBody($terminalBody));
    systemCheck($wireBody === $expectedWireBody,
        'Build must preserve zero, one, or multiple terminal LF bytes after normalization');
    systemCheck($auth->isAuthentic($terminalWire),
        'Every preserved terminal-LF vector must verify its hash and HMAC');
}

$recovery = $auth->build('recovery', ['z@EXAMPLE.INVALID', 'a@example.invalid', 'a@example.invalid'], $date,
    '0123456789abcdef0123456789abcdef', "Recovered");
systemCheck(str_starts_with($recovery,
    "To: a@example.invalid,z@example.invalid\r\nSubject: Xserver mail notifier recovered\r\n"),
    'Build must byte-sort and deduplicate canonical recipients and map recovery subject');
systemCheck(str_ends_with($recovery, "\r\n\r\nRecovered"),
    'Build must not add a terminal CRLF');
systemCheck($auth->isAuthentic($recovery), 'Recovery wire must verify');
systemRejects($auth, syntheticSystemMail($key, "A\nB\n", [], 'error',
    '0123456789abcdef0123456789abcdef', 'operator@example.invalid', $date, null, "\n"),
    'LF-only header/body boundary must be rejected');
systemCheck($auth->isAuthentic(syntheticSystemMail($key, "A\nB\n")),
    'LF body bytes after a valid CRLF boundary must normalize and verify');
systemCheck($auth->isAuthentic(syntheticSystemMail($key, "A\rB\r")),
    'Lone-CR body wire must normalize without changing terminal semantics');

systemRejects($auth, str_replace("Failure\r\n", "Changed\r\n", $wire), 'Body mutation must fail');
systemRejects($auth, "X-Xserver-Mail-Notifier-Version: 1\r\n\r\nbody", 'Fixed header alone must fail');
systemRejects($auth, syntheticSystemMail($key, "Failure\r\n") . 'x', 'Body replay with different terminal bytes must fail');
systemRejects($auth, str_replace('operator@example.invalid', 'other@example.invalid', $wire), 'To mutation must fail');
systemRejects($auth, str_replace($date, 'Tue, 14 Jul 2026 12:00:00 +0000', $wire), 'Date mutation must fail');
systemRejects($auth, str_replace($event, str_repeat('b', 32), $wire), 'Event mutation must fail');
systemRejects($auth, str_replace($expectedHmac, str_repeat('0', 64), $wire), 'Forged HMAC must fail');
systemRejects($auth, str_replace('Xserver mail notifier error', 'Xserver mail notifier recovered', $wire),
    'Type and Subject mismatch must fail');
systemRejects($auth, str_replace('Notifier-Type: error', 'Notifier-Type: invalid', $wire), 'Invalid Type must fail');
systemRejects($auth, str_replace($event, strtoupper($event), $wire), 'Event must be lowercase hexadecimal');
systemRejects($auth, str_replace($bodyHash, strtoupper($bodyHash), $wire), 'Body hash must be lowercase hexadecimal');
systemRejects($auth, str_replace($expectedHmac, strtoupper($expectedHmac), $wire), 'HMAC must be lowercase hexadecimal');
systemRejects($auth, str_replace("Failure\r\n", "\xff\r\n", $wire), 'Invalid UTF-8 body must fail');
systemRejects($auth, "X-Bad: \xff\r\n" . $wire, 'Invalid UTF-8 header bytes must fail');
foreach (["\0Mon, 13 Jul 2026 12:00:00 +0000", "Mon, 13 Jul\x01 2026 12:00:00 +0000"] as $badDate) {
    systemRejects($auth, syntheticSystemMail($key, "Body\r\n", date: $badDate),
        'Date control bytes must return false without throwing');
}

$requiredLines = explode("\r\n", strstr($wire, "\r\n\r\n", true));
foreach ($requiredLines as $line) {
    [$name] = explode(':', $line, 2);
    systemRejects($auth, str_replace($line . "\r\n", '', $wire), 'Missing required header must fail: ' . $name);
    systemRejects($auth, $line . "\r\n" . $wire, 'Duplicate required header must fail: ' . $name);
    systemRejects($auth, str_replace($line, strtoupper($name) . substr($line, strlen($name))
        . "\r\n folded", $wire), 'Folded required header must fail: ' . $name);
}

$caseVariant = str_replace('X-Xserver-Mail-Notifier-Version:', 'x-xserver-mail-notifier-version:', $wire);
systemCheck($auth->isAuthentic($caseVariant), 'Required header names must be case-insensitive');
systemRejects($auth, "x-xserver-mail-notifier-version: 1\r\n" . $wire,
    'Case-variant duplicate required header must fail');
$foldedUnrelated = "X-Transport-Trace: first\r\n second\r\n" . $wire;
systemCheck($auth->isAuthentic($foldedUnrelated),
    'RFC folding on an unrelated transport header must not fold a required authenticated header');
systemCheck($auth->isAuthentic("X-Test:value\r\nX-Empty:\r\n" . $wire),
    'Unrelated RFC fields may omit optional whitespace and may have empty values');
systemRejects($auth, str_replace('To: operator@example.invalid', 'To:operator@example.invalid', $wire),
    'Required authenticated fields must retain exact colon-space syntax');

$to997 = recipientsOfLength(993);
$line997 = syntheticSystemMail($key, "Body\r\n", [], 'error',
    '0123456789abcdef0123456789abcdef', $to997);
systemCheck(strlen(strtok($line997, "\r\n")) === 997, 'Boundary fixture must contain a 997-byte required To line');
systemCheck($auth->isAuthentic($line997), 'A 997-byte raw authenticated header line must be accepted');
$to998 = recipientsOfLength(994);
$line998 = syntheticSystemMail($key, "Body\r\n", [], 'error',
    '0123456789abcdef0123456789abcdef', $to998);
systemCheck(strlen(strtok($line998, "\r\n")) === 998, 'Boundary fixture must contain a 998-byte required To line');
systemRejects($auth, $line998, 'A 998-byte raw authenticated header line must be rejected');

$base = syntheticSystemMail($key, "Body\r\n");
$basePrefix = strstr($base, "\r\n\r\n", true) . "\r\n\r\n";
$prefix65536 = syntheticSystemMail($key, "Body\r\n", paddingHeadersForPrefix($basePrefix, 65536));
$boundary = strpos($prefix65536, "\r\n\r\n");
systemCheck($boundary !== false && $boundary + 4 === 65536, 'Accepted prefix fixture must end at exactly 65536 bytes');
systemCheck($auth->isAuthentic($prefix65536), 'Authenticated header prefix of exactly 65536 bytes must be scanned');
$prefix65537 = syntheticSystemMail($key, "Body\r\n", paddingHeadersForPrefix($basePrefix, 65537));
$boundary = strpos($prefix65537, "\r\n\r\n");
systemCheck($boundary !== false && $boundary + 4 === 65537, 'Rejected prefix fixture must end at exactly 65537 bytes');
systemRejects($auth, $prefix65537, 'Authenticated header prefix of 65537 bytes must not be scanned');

foreach ([str_repeat('k', 31), str_repeat('k', 33)] as $badKey) {
    try {
        new SystemMailAuthenticator($badKey);
        throw new RuntimeException('Invalid decoded key length was accepted');
    } catch (InvalidArgumentException $error) {
        systemCheck($error->getMessage() === 'Invalid system mail key', 'Key error must be fixed');
    }
}
foreach ([
    ['bad', ['operator@example.invalid'], $date, $event, 'body'],
    ['error', ['Name <operator@example.invalid>'], $date, $event, 'body'],
    ['error', ['operator@example.invalid'], 'Mon, 13 Jul 2026 12:00:00 +0900', $event, 'body'],
    ['error', ['operator@example.invalid'], $date, str_repeat('A', 32), 'body'],
    ['error', ['operator@example.invalid'], $date, $event, "\xff"],
] as $invalidBuild) {
    try {
        $auth->build(...$invalidBuild);
        throw new RuntimeException('Invalid build input was accepted');
    } catch (InvalidArgumentException $error) {
        systemCheck($error->getMessage() === 'Invalid system mail input', 'Build error must be fixed');
    }
}

echo "PASS: authenticated system mail wire contract\n";
