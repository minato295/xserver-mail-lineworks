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

/** @return array<string,array{type:string,test:bool,text:string,encoded:string}> */
function v2Subjects(): array
{
    $values = [
        'error-real' => ['error', false, '【要確認】LINE WORKSメール通知で障害が発生しました'],
        'recovery-real' => ['recovery', false, '【復旧・要確認】LINE WORKSメール通知が復旧しました'],
        'error-test' => ['error', true, '【テスト・対応不要】障害通知メールの動作確認'],
        'recovery-test' => ['recovery', true, '【テスト・対応不要】復旧通知メールの動作確認'],
    ];
    $result = [];
    foreach ($values as $name => [$type, $test, $text]) {
        $chunks = [];
        $chunk = '';
        $points = preg_split('//u', $text, -1, PREG_SPLIT_NO_EMPTY);
        systemCheck(is_array($points), 'Subject test vector must be valid UTF-8');
        foreach ($points as $point) {
            if ($chunk !== '' && strlen($chunk . $point) > 45) {
                $chunks[] = $chunk;
                $chunk = '';
            }
            $chunk .= $point;
        }
        if ($chunk !== '') {
            $chunks[] = $chunk;
        }
        $encoded = implode(' ', array_map(
            static fn (string $value): string => '=?UTF-8?B?' . base64_encode($value) . '?=',
            $chunks,
        ));
        $result[$name] = ['type' => $type, 'test' => $test, 'text' => $text, 'encoded' => $encoded];
    }
    return $result;
}

function canonicalBase64(string $decoded): string
{
    return rtrim(chunk_split(base64_encode($decoded), 76, "\r\n"), "\r\n") . "\r\n";
}

function syntheticV2(
    string $key,
    string $decodedBody,
    string $type = 'error',
    string $event = '0123456789abcdef0123456789abcdef',
    string $to = 'operator@example.invalid',
    string $date = 'Tue, 14 Jul 2026 17:33:55 +0900',
    ?string $subject = null,
    string $version = '2',
    string $mimeVersion = '1.0',
    string $contentType = 'text/plain; charset=UTF-8',
    string $transferEncoding = 'base64',
    ?string $wireBody = null,
): string {
    $subject ??= v2Subjects()['error-real']['encoded'];
    $bodyHash = hash('sha256', $decodedBody, false);
    $hmac = framedHmac($key, [
        $version, $type, $event, $to, $subject, $date,
        $mimeVersion, $contentType, $transferEncoding, $bodyHash,
    ]);
    return "To: {$to}\r\n"
        . "Subject: {$subject}\r\n"
        . "Date: {$date}\r\n"
        . "MIME-Version: {$mimeVersion}\r\n"
        . "Content-Type: {$contentType}\r\n"
        . "Content-Transfer-Encoding: {$transferEncoding}\r\n"
        . "X-Xserver-Mail-Notifier-Version: {$version}\r\n"
        . "X-Xserver-Mail-Notifier-Type: {$type}\r\n"
        . "X-Xserver-Mail-Notifier-Event: {$event}\r\n"
        . "X-Xserver-Mail-Notifier-Body-SHA256: {$bodyHash}\r\n"
        . "X-Xserver-Mail-Notifier-Auth: hmac-sha256={$hmac}\r\n\r\n"
        . ($wireBody ?? canonicalBase64($decodedBody));
}

function wireBody(string $wire): string
{
    $offset = strpos($wire, "\r\n\r\n");
    systemCheck($offset !== false, 'Wire must have a CRLF header boundary');
    return substr($wire, $offset + 4);
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
$auth = new SystemMailAuthenticator($keyBytes);
$date = 'Tue, 14 Jul 2026 17:33:55 +0900';
$event = str_repeat('a', 32);
$body = "LINE WORKSへの通知で障害が発生しました。\n";
$subjects = v2Subjects();
$encodedSubject = $subjects['error-real']['encoded'];
$wire = $auth->build('error', ['operator@example.invalid'], $date, $event, $body);
$bodyHash = hash('sha256', $body, false);
$expectedHmac = framedHmac($keyBytes, [
    '2', 'error', $event, 'operator@example.invalid', $encodedSubject, $date,
    '1.0', 'text/plain; charset=UTF-8', 'base64', $bodyHash,
]);
$expected = "To: operator@example.invalid\r\n"
    . "Subject: {$encodedSubject}\r\n"
    . "Date: {$date}\r\n"
    . "MIME-Version: 1.0\r\n"
    . "Content-Type: text/plain; charset=UTF-8\r\n"
    . "Content-Transfer-Encoding: base64\r\n"
    . "X-Xserver-Mail-Notifier-Version: 2\r\n"
    . "X-Xserver-Mail-Notifier-Type: error\r\n"
    . "X-Xserver-Mail-Notifier-Event: {$event}\r\n"
    . "X-Xserver-Mail-Notifier-Body-SHA256: {$bodyHash}\r\n"
    . "X-Xserver-Mail-Notifier-Auth: hmac-sha256={$expectedHmac}\r\n\r\n"
    . canonicalBase64($body);
systemCheck($wire === $expected, 'v2 wire order, independent HMAC framing, and canonical MIME bytes must be exact');
systemCheck($auth->isAuthentic($wire), 'Generated v2 wire must authenticate');

foreach ($subjects as $name => $subjectVector) {
    $subjectWire = $auth->build(
        $subjectVector['type'],
        ['z@EXAMPLE.INVALID', 'a@example.invalid', 'a@example.invalid'],
        $date,
        '0123456789abcdef0123456789abcdef',
        "本文\n",
        $subjectVector['test'],
    );
    systemCheck(str_starts_with($subjectWire, "To: a@example.invalid,z@example.invalid\r\n"),
        'v2 To must sort and deduplicate: ' . $name);
    systemCheck(str_contains($subjectWire, "Subject: {$subjectVector['encoded']}\r\n"),
        'v2 must use the exact canonical subject: ' . $name);
    systemCheck(substr_count($subjectWire, "MIME-Version: 1.0\r\n") === 1
        && substr_count($subjectWire, "Content-Type: text/plain; charset=UTF-8\r\n") === 1
        && substr_count($subjectWire, "Content-Transfer-Encoding: base64\r\n") === 1,
        'v2 MIME headers must occur exactly once: ' . $name);
    systemCheck($auth->isAuthentic($subjectWire), 'Every approved v2 type/subject pair must authenticate: ' . $name);
    foreach (explode(' ', $subjectVector['encoded']) as $word) {
        systemCheck(strlen($word) <= 75, 'Each RFC 2047 encoded-word must be at most 75 bytes');
        systemCheck(preg_match('/\A=\?UTF-8\?B\?(.+)\?=\z/D', $word, $match) === 1,
            'Every subject word must use exact UTF-8 Base64 syntax');
        $decodedChunk = base64_decode($match[1], true);
        systemCheck(is_string($decodedChunk) && strlen($decodedChunk) <= 45 && preg_match('//u', $decodedChunk) === 1,
            'Subject chunks must be valid UTF-8 codepoint sequences of at most 45 bytes');
    }
}

$encodedLines = explode("\r\n", rtrim(wireBody($wire), "\r\n"));
foreach ($encodedLines as $line) {
    systemCheck(strlen($line) <= 76, 'Canonical Base64 lines must be at most 76 bytes');
}
systemCheck(str_ends_with(wireBody($wire), "\r\n") && !str_ends_with(wireBody($wire), "\r\n\r\n"),
    'Canonical Base64 must have exactly one terminal CRLF');

$nfc = "caf\xC3\xA9\n";
$nfd = "cafe\xCC\x81\n";
$nfcWire = $auth->build('error', ['operator@example.invalid'], $date, $event, $nfc);
$nfdWire = $auth->build('error', ['operator@example.invalid'], $date, $event, $nfd);
systemCheck(base64_decode(str_replace("\r\n", '', wireBody($nfcWire)), true) === $nfc
    && base64_decode(str_replace("\r\n", '', wireBody($nfdWire)), true) === $nfd
    && $nfcWire !== $nfdWire && $auth->isAuthentic($nfcWire) && $auth->isAuthentic($nfdWire),
    'NFC and NFD decoded body bytes must remain distinct and authenticate without normalization');

$legacyKey = str_repeat('k', 32);
$legacyAuth = new SystemMailAuthenticator($legacyKey);
$fixture = file_get_contents(dirname(__DIR__) . '/fixtures/system-mail-v1-postfix.eml');
systemCheck(is_string($fixture), 'Fixed Postfix v1 fixture must be readable');
$fixture = str_replace("\n", "\r\n", str_replace("\r\n", "\n", $fixture));
systemCheck(substr_count($fixture, 'Received: ') === 2
    && str_contains($fixture, "\r\n\tby mx.example.invalid")
    && str_contains($fixture, 'Return-Path: <notifier@example.invalid>')
    && str_contains($fixture, 'From: notifier@example.invalid')
    && str_contains($fixture, 'Message-Id: <legacy-v1@example.invalid>')
    && !str_contains($fixture, 'MIME-Version:') && !str_contains($fixture, 'Content-Type:'),
    'Fixed v1 fixture must contain realistic delivery headers and no MIME headers');
systemCheck($legacyAuth->isAuthentic($fixture), 'Fixed genuine Postfix-style v1 fixture must authenticate');
systemRejects($legacyAuth, str_replace('Legacy delivery failure', 'Legacy delivery failurE', $fixture),
    'A one-byte v1 fixture body mutation must fail');
systemRejects($legacyAuth, str_replace('39b0d590', '39b0d591', $fixture),
    'A one-byte v1 fixture HMAC mutation must fail');

$v1Date = 'Mon, 13 Jul 2026 12:00:00 +0000';
$v1 = syntheticSystemMail($legacyKey, "Failure\n");
$v1BodyHash = hash('sha256', "Failure\n", false);
$v1ExpectedHmac = framedHmac($legacyKey, [
    '1', 'error', '0123456789abcdef0123456789abcdef', 'operator@example.invalid',
    'Xserver mail notifier error', $v1Date, $v1BodyHash,
]);
$v1Expected = "To: operator@example.invalid\r\n"
    . "Subject: Xserver mail notifier error\r\n"
    . "Date: {$v1Date}\r\n"
    . "X-Xserver-Mail-Notifier-Version: 1\r\n"
    . "X-Xserver-Mail-Notifier-Type: error\r\n"
    . "X-Xserver-Mail-Notifier-Event: 0123456789abcdef0123456789abcdef\r\n"
    . "X-Xserver-Mail-Notifier-Body-SHA256: {$v1BodyHash}\r\n"
    . "X-Xserver-Mail-Notifier-Auth: hmac-sha256={$v1ExpectedHmac}\r\n\r\nFailure\n";
systemCheck($v1 === $v1Expected, 'Synthetic v1 exact wire and HMAC vector must remain byte-compatible');
systemCheck($legacyAuth->isAuthentic($v1), 'Synthetic v1 exact HMAC vector must remain authentic');
systemCheck($legacyAuth->isAuthentic(syntheticSystemMail($legacyKey, "A\rB\r")),
    'v1 lone-CR body normalization must remain byte-compatible');
systemCheck($legacyAuth->isAuthentic(syntheticSystemMail($legacyKey, "A\nB\n")),
    'v1 LF body normalization must remain byte-compatible');
foreach (["No terminal LF", "One terminal LF\n", "Two terminal LFs\n\n"] as $terminalBody) {
    systemCheck($legacyAuth->isAuthentic(syntheticSystemMail($legacyKey, $terminalBody)),
        'v1 must preserve every existing terminal-LF signing semantic');
}
systemRejects($legacyAuth, str_replace("Failure\n", "Changed\n", $v1), 'v1 body mutation must fail');
systemRejects($legacyAuth, syntheticSystemMail($legacyKey, "Failure\n") . 'x',
    'v1 body replay with different terminal bytes must fail');
systemRejects($legacyAuth, str_replace('operator@example.invalid', 'other@example.invalid', $v1), 'v1 To mutation must fail');
systemRejects($legacyAuth, str_replace($v1Date, 'Tue, 14 Jul 2026 12:00:00 +0000', $v1), 'v1 Date mutation must fail');
systemRejects($legacyAuth, str_replace('0123456789abcdef0123456789abcdef', str_repeat('b', 32), $v1),
    'v1 event mutation must fail');
systemRejects($legacyAuth, str_replace($v1ExpectedHmac, str_repeat('0', 64), $v1), 'v1 forged HMAC must fail');
systemRejects($legacyAuth, str_replace('Xserver mail notifier error', 'Xserver mail notifier recovered', $v1),
    'v1 type and subject mismatch must fail');
systemRejects($legacyAuth, str_replace('Notifier-Type: error', 'Notifier-Type: invalid', $v1), 'v1 invalid type must fail');
systemRejects($legacyAuth, str_replace('0123456789abcdef0123456789abcdef', strtoupper('0123456789abcdef0123456789abcdef'), $v1),
    'v1 event must remain lowercase hexadecimal');
systemRejects($legacyAuth, str_replace($v1BodyHash, strtoupper($v1BodyHash), $v1),
    'v1 body hash must remain lowercase hexadecimal');
systemRejects($legacyAuth, str_replace($v1ExpectedHmac, strtoupper($v1ExpectedHmac), $v1),
    'v1 HMAC must remain lowercase hexadecimal');
systemRejects($legacyAuth, str_replace("Failure\n", "\xff\n", $v1), 'v1 invalid UTF-8 body must fail');
systemRejects($legacyAuth, "X-Bad: \xff\r\n" . $v1, 'v1 invalid UTF-8 headers must fail');
systemRejects($legacyAuth, syntheticSystemMail($legacyKey, "A\nB\n", lineEnd: "\n"), 'v1 LF-only boundary must fail');
foreach (["\0Mon, 13 Jul 2026 12:00:00 +0000", "Mon, 13 Jul\x01 2026 12:00:00 +0000"] as $badDate) {
    systemRejects($legacyAuth, syntheticSystemMail($legacyKey, "Body\n", date: $badDate),
        'v1 Date control bytes must return false without throwing');
}

$v1RequiredLines = explode("\r\n", strstr($v1, "\r\n\r\n", true));
foreach ($v1RequiredLines as $line) {
    [$name] = explode(':', $line, 2);
    systemRejects($legacyAuth, str_replace($line . "\r\n", '', $v1), 'Missing v1 required header must fail: ' . $name);
    systemRejects($legacyAuth, $line . "\r\n" . $v1, 'Duplicate v1 required header must fail: ' . $name);
    systemRejects($legacyAuth, str_replace($line, strtoupper($name) . substr($line, strlen($name))
        . "\r\n folded", $v1), 'Folded v1 required header must fail: ' . $name);
}
$v1CaseVariant = str_replace('X-Xserver-Mail-Notifier-Version:', 'x-xserver-mail-notifier-version:', $v1);
systemCheck($legacyAuth->isAuthentic($v1CaseVariant), 'v1 required header names must remain case-insensitive');
systemRejects($legacyAuth, "x-xserver-mail-notifier-version: 1\r\n" . $v1,
    'v1 case-variant duplicate required header must fail');
systemCheck($legacyAuth->isAuthentic("X-Transport-Trace: first\r\n second\r\n" . $v1),
    'v1 unrelated folded transport headers must remain accepted');
systemCheck($legacyAuth->isAuthentic("MIME-Version: legacy\r\n folded\r\n" . $v1),
    'v1 must not acquire v2 MIME syntax requirements');
systemCheck($legacyAuth->isAuthentic("X-Test:value\r\nX-Empty:\r\n" . $v1),
    'v1 unrelated RFC fields may omit whitespace and have empty values');
systemRejects($legacyAuth, str_replace('To: operator@example.invalid', 'To:operator@example.invalid', $v1),
    'v1 authenticated fields must retain exact colon-space syntax');

$v1To997 = recipientsOfLength(993);
$v1Line997 = syntheticSystemMail($legacyKey, "Body\n", to: $v1To997);
systemCheck(strlen(strtok($v1Line997, "\r\n")) === 997 && $legacyAuth->isAuthentic($v1Line997),
    'A 997-byte v1 authenticated header line must remain accepted');
$v1To998 = recipientsOfLength(994);
$v1Line998 = syntheticSystemMail($legacyKey, "Body\n", to: $v1To998);
systemRejects($legacyAuth, $v1Line998, 'A 998-byte v1 authenticated header line must remain rejected');

$v1BasePrefix = strstr(syntheticSystemMail($legacyKey, "Body\n"), "\r\n\r\n", true) . "\r\n\r\n";
$v1Prefix65536 = syntheticSystemMail($legacyKey, "Body\n", paddingHeadersForPrefix($v1BasePrefix, 65_536));
systemCheck(strpos($v1Prefix65536, "\r\n\r\n") + 4 === 65_536 && $legacyAuth->isAuthentic($v1Prefix65536),
    'v1 header prefix of exactly 65536 bytes must remain scanned');
$v1Prefix65537 = syntheticSystemMail($legacyKey, "Body\n", paddingHeadersForPrefix($v1BasePrefix, 65_537));
systemRejects($legacyAuth, $v1Prefix65537, 'v1 header prefix of 65537 bytes must remain outside the scan limit');

$longBody = str_repeat('x', 100) . "\n";
$validTamperWire = syntheticV2($legacyKey, $longBody);
$otherChunkSubject = str_replace(' ', '  ', $subjects['error-real']['encoded']);
$qSubject = '=?UTF-8?Q?' . rawurlencode($subjects['error-real']['text']) . '?=';
$rawSubject = $subjects['error-real']['text'];
$lowerCharsetSubject = str_replace('UTF-8', 'utf-8', $subjects['error-real']['encoded']);
$oneWordSubject = '=?UTF-8?B?' . base64_encode($subjects['error-real']['text']) . '?=';
$mismatchWire = syntheticV2($legacyKey, $longBody, 'error', subject: $subjects['recovery-real']['encoded']);
$subjectTamper = [
    'type/subject mismatch' => $mismatchWire,
    'raw UTF-8 subject' => syntheticV2($legacyKey, $longBody, subject: $rawSubject),
    'Q subject encoding' => syntheticV2($legacyKey, $longBody, subject: $qSubject),
    'different subject chunks' => syntheticV2($legacyKey, $longBody, subject: $oneWordSubject),
    'different subject charset case' => syntheticV2($legacyKey, $longBody, subject: $lowerCharsetSubject),
    'different encoded-word spacing' => syntheticV2($legacyKey, $longBody, subject: $otherChunkSubject),
];
foreach ($subjectTamper as $name => $candidate) {
    systemRejects($legacyAuth, $candidate, 'v2 must reject ' . $name);
}

foreach ([
    'UTC offset' => 'Tue, 14 Jul 2026 08:33:55 +0000',
    'wrong weekday' => 'Mon, 14 Jul 2026 17:33:55 +0900',
    'impossible date' => 'Fri, 31 Apr 2026 17:33:55 +0900',
    'short digit' => 'Tue, 14 Jul 2026 7:33:55 +0900',
    'control byte' => "Tue, 14 Jul 2026 17:33:55 +0900\x01",
] as $name => $badDate) {
    systemRejects($legacyAuth, syntheticV2($legacyKey, $longBody, date: $badDate), 'v2 must reject Date ' . $name);
}

$mimeMutations = [];
foreach ([
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
    'Content-Transfer-Encoding: base64',
] as $mimeLine) {
    $mimeMutations['missing ' . $mimeLine] = str_replace($mimeLine . "\r\n", '', $validTamperWire);
    $mimeMutations['duplicate ' . $mimeLine] = $mimeLine . "\r\n" . $validTamperWire;
    $mimeMutations['folded ' . $mimeLine] = str_replace(
        $mimeLine . "\r\n",
        $mimeLine . "\r\n folded\r\n",
        $validTamperWire,
    );
}
$mimeMutations += [
    'wrong MIME-Version' => syntheticV2($legacyKey, $longBody, mimeVersion: '1.00'),
    'wrong Content-Type' => syntheticV2($legacyKey, $longBody, contentType: 'text/plain;charset=UTF-8'),
    'wrong charset' => syntheticV2($legacyKey, $longBody, contentType: 'text/plain; charset=utf-8'),
    'wrong transfer encoding' => syntheticV2($legacyKey, $longBody, transferEncoding: 'Base64'),
];
foreach ($mimeMutations as $name => $candidate) {
    systemRejects($legacyAuth, $candidate, 'v2 must reject ' . $name);
}

$v2RequiredLines = array_values(array_filter(
    explode("\r\n", strstr($validTamperWire, "\r\n\r\n", true)),
    static fn (string $line): bool => !str_starts_with($line, 'MIME-Version:')
        && !str_starts_with($line, 'Content-Type:')
        && !str_starts_with($line, 'Content-Transfer-Encoding:'),
));
foreach ($v2RequiredLines as $line) {
    [$name] = explode(':', $line, 2);
    systemRejects($legacyAuth, str_replace($line . "\r\n", '', $validTamperWire),
        'Missing v2 common header must fail: ' . $name);
    systemRejects($legacyAuth, $line . "\r\n" . $validTamperWire,
        'Duplicate v2 common header must fail: ' . $name);
    systemRejects($legacyAuth, str_replace($line, $line . "\r\n folded", $validTamperWire),
        'Folded v2 common header must fail: ' . $name);
}

$canonicalBodyWire = canonicalBase64($longBody);
$flat = str_replace("\r\n", '', $canonicalBodyWire);
$base64Mutations = [
    'invalid Base64 character' => substr_replace($canonicalBodyWire, '*', 10, 1),
    'Base64 whitespace' => substr_replace($canonicalBodyWire, ' ', 10, 0),
    'Base64 padding' => rtrim($canonicalBodyWire, "\r\n") . "=\r\n",
    'LF-only Base64' => str_replace("\r\n", "\n", $canonicalBodyWire),
    '75-column Base64' => rtrim(chunk_split($flat, 75, "\r\n"), "\r\n") . "\r\n",
    '77-column Base64' => rtrim(chunk_split($flat, 77, "\r\n"), "\r\n") . "\r\n",
    'missing terminal CRLF' => rtrim($canonicalBodyWire, "\r\n"),
    'duplicate terminal CRLF' => $canonicalBodyWire . "\r\n",
];
foreach ($base64Mutations as $name => $badWireBody) {
    systemRejects($legacyAuth, syntheticV2($legacyKey, $longBody, wireBody: $badWireBody), 'v2 must reject ' . $name);
}

foreach ([
    'invalid UTF-8' => "bad\xff\n",
    'NUL' => "bad\0\n",
    'CR' => "bad\r\n",
    'no terminal LF' => 'bad',
    'two terminal LFs' => "bad\n\n",
] as $name => $badDecodedBody) {
    systemRejects($legacyAuth, syntheticV2($legacyKey, $badDecodedBody), 'v2 must reject decoded body ' . $name);
}

$bodyHash = hash('sha256', $longBody);
$hmac = substr(strstr($validTamperWire, 'hmac-sha256='), 12, 64);
foreach ([
    'body hash' => str_replace($bodyHash, str_repeat('0', 64), $validTamperWire),
    'HMAC' => str_replace($hmac, str_repeat('0', 64), $validTamperWire),
    'event' => str_replace('0123456789abcdef0123456789abcdef', str_repeat('b', 32), $validTamperWire),
    'To' => str_replace('operator@example.invalid', 'other@example.invalid', $validTamperWire),
    'Date' => str_replace('17:33:55', '17:33:56', $validTamperWire),
] as $name => $candidate) {
    systemRejects($legacyAuth, $candidate, 'v2 must reject authenticated ' . $name . ' mutation');
}
foreach (['0', '3', '02'] as $badVersion) {
    systemRejects($legacyAuth, str_replace('Notifier-Version: 2', 'Notifier-Version: ' . $badVersion, $validTamperWire),
        'v2 must reject version ' . $badVersion);
}
systemRejects($legacyAuth, str_replace("X-Xserver-Mail-Notifier-Version: 2\r\n", '', $validTamperWire),
    'v2 must reject a missing version');
systemRejects($legacyAuth, "X-Xserver-Mail-Notifier-Version: 2\r\n" . $validTamperWire,
    'v2 must reject a duplicate version');

$to997 = recipientsOfLength(993);
$recipients997 = explode(',', $to997);
$line997 = $auth->build('error', $recipients997, $date, $event, "Body\n");
systemCheck(strlen(strtok($line997, "\r\n")) === 997 && $auth->isAuthentic($line997),
    'A 997-byte v2 required To line must be accepted');
$to998 = recipientsOfLength(994);
try {
    $auth->build('error', explode(',', $to998), $date, $event, "Body\n");
    throw new RuntimeException('A 998-byte v2 required To line was accepted');
} catch (InvalidArgumentException $error) {
    systemCheck($error->getMessage() === 'Invalid system mail input', '998-byte v2 header error must be fixed');
}

$sizeVectors = [];
for ($bodyLength = 47_000; $bodyLength <= 50_000 && count($sizeVectors) < 2; ++$bodyLength) {
    $sizedBody = str_repeat('s', $bodyLength - 1) . "\n";
    foreach (range(3, 64) as $localLength) {
        $recipient = str_repeat('r', $localLength) . '@example.invalid';
        $candidate = syntheticV2($keyBytes, $sizedBody, event: $event, to: $recipient);
        $length = strlen($candidate);
        if ($length === 65_536 || $length === 65_537) {
            $sizeVectors[$length] = [$recipient, $sizedBody];
        }
    }
}
systemCheck(isset($sizeVectors[65_536], $sizeVectors[65_537]), 'Independent vectors must reach both wire size boundaries');
[$recipient65536, $body65536] = $sizeVectors[65_536];
systemCheck(strlen($auth->build('error', [$recipient65536], $date, $event, $body65536)) === 65_536,
    'A canonical post-Base64 v2 wire of exactly 65536 bytes must be accepted');
[$recipient65537, $body65537] = $sizeVectors[65_537];
try {
    $auth->build('error', [$recipient65537], $date, $event, $body65537);
    throw new RuntimeException('A canonical post-Base64 v2 wire of 65537 bytes was accepted');
} catch (InvalidArgumentException $error) {
    systemCheck($error->getMessage() === 'Invalid system mail input', '65537-byte v2 wire error must be fixed');
}

foreach ([str_repeat('k', 31), str_repeat('k', 33)] as $badKey) {
    try {
        new SystemMailAuthenticator($badKey);
        throw new RuntimeException('Invalid decoded key length was accepted');
    } catch (InvalidArgumentException $error) {
        systemCheck($error->getMessage() === 'Invalid system mail key', 'Key error must be fixed');
    }
}
foreach ([
    ['bad', ['operator@example.invalid'], $date, $event, "body\n"],
    ['error', ['Name <operator@example.invalid>'], $date, $event, "body\n"],
    ['error', ['operator@example.invalid'], 'Tue, 14 Jul 2026 08:33:55 +0000', $event, "body\n"],
    ['error', ['operator@example.invalid'], $date, str_repeat('A', 32), "body\n"],
    ['error', ['operator@example.invalid'], $date, $event, "body"],
    ['error', ['operator@example.invalid'], $date, $event, "body\n\n"],
    ['error', ['operator@example.invalid'], $date, $event, "body\r\n"],
    ['error', ['operator@example.invalid'], $date, $event, "body\0\n"],
    ['error', ['operator@example.invalid'], $date, $event, "\xff\n"],
] as $invalidBuild) {
    try {
        $auth->build(...$invalidBuild);
        throw new RuntimeException('Invalid v2 build input was accepted');
    } catch (InvalidArgumentException $error) {
        systemCheck($error->getMessage() === 'Invalid system mail input', 'Build error must be fixed');
    }
}

echo "PASS: authenticated system mail v1/v2 wire contract\n";
