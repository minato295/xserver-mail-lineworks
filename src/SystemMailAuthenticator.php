<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeImmutable;
use DateTimeZone;
use InvalidArgumentException;

final class SystemMailAuthenticator
{
    private const MAX_HEADER_PREFIX_BYTES = 65_536;
    private const MAX_HEADER_LINE_BYTES = 997;
    private const MAX_MESSAGE_BYTES = 65_536;

    /** @var array<string,string> */
    private const V1_SUBJECTS = [
        'error' => 'Xserver mail notifier error',
        'recovery' => 'Xserver mail notifier recovered',
    ];

    /** @var array<string,array{real:string,test:string}> */
    private const V2_SUBJECTS = [
        'error' => [
            'real' => '【要確認】LINE WORKSメール通知で障害が発生しました',
            'test' => '【テスト・対応不要】障害通知メールの動作確認',
        ],
        'recovery' => [
            'real' => '【復旧・要確認】LINE WORKSメール通知が復旧しました',
            'test' => '【テスト・対応不要】復旧通知メールの動作確認',
        ],
    ];

    /** @var list<string> */
    private const REQUIRED_HEADERS = [
        'to',
        'subject',
        'date',
        'x-xserver-mail-notifier-version',
        'x-xserver-mail-notifier-type',
        'x-xserver-mail-notifier-event',
        'x-xserver-mail-notifier-body-sha256',
        'x-xserver-mail-notifier-auth',
    ];

    /** @var array<string,string> */
    private const V2_MIME_HEADERS = [
        'mime-version' => '1.0',
        'content-type' => 'text/plain; charset=UTF-8',
        'content-transfer-encoding' => 'base64',
    ];

    public function __construct(private readonly string $key)
    {
        if (strlen($key) !== 32) {
            throw new InvalidArgumentException('Invalid system mail key');
        }
    }

    /** @param list<string> $recipients */
    public function build(
        string $type,
        array $recipients,
        string $date,
        string $eventId,
        string $body,
        bool $test = false,
    ): string {
        try {
            $to = implode(',', CanonicalEmail::many($recipients, false));
        } catch (InvalidArgumentException) {
            throw new InvalidArgumentException('Invalid system mail input');
        }
        if (!isset(self::V2_SUBJECTS[$type])
            || preg_match('/\A[0-9a-f]{32}\z/D', $eventId) !== 1
            || !$this->validV2Date($date)
            || !$this->validV2Body($body)) {
            throw new InvalidArgumentException('Invalid system mail input');
        }

        $subject = $this->encodeSubject(self::V2_SUBJECTS[$type][$test ? 'test' : 'real']);
        $bodyHash = hash('sha256', $body, false);
        $hmac = $this->hmac([
            '2', $type, $eventId, $to, $subject, $date,
            self::V2_MIME_HEADERS['mime-version'],
            self::V2_MIME_HEADERS['content-type'],
            self::V2_MIME_HEADERS['content-transfer-encoding'],
            $bodyHash,
        ]);
        $lines = [
            'To: ' . $to,
            'Subject: ' . $subject,
            'Date: ' . $date,
            'MIME-Version: ' . self::V2_MIME_HEADERS['mime-version'],
            'Content-Type: ' . self::V2_MIME_HEADERS['content-type'],
            'Content-Transfer-Encoding: ' . self::V2_MIME_HEADERS['content-transfer-encoding'],
            'X-Xserver-Mail-Notifier-Version: 2',
            'X-Xserver-Mail-Notifier-Type: ' . $type,
            'X-Xserver-Mail-Notifier-Event: ' . $eventId,
            'X-Xserver-Mail-Notifier-Body-SHA256: ' . $bodyHash,
            'X-Xserver-Mail-Notifier-Auth: hmac-sha256=' . $hmac,
        ];
        foreach ($lines as $line) {
            if (strlen($line) > self::MAX_HEADER_LINE_BYTES) {
                throw new InvalidArgumentException('Invalid system mail input');
            }
        }
        $wire = implode("\r\n", $lines) . "\r\n\r\n" . $this->canonicalBase64($body);
        if (strlen($wire) > self::MAX_MESSAGE_BYTES) {
            throw new InvalidArgumentException('Invalid system mail input');
        }
        return $wire;
    }

    public function isAuthentic(string $raw): bool
    {
        $boundary = $this->headerBoundary($raw);
        if ($boundary === null) {
            return false;
        }
        $headers = $this->parseHeaders(substr($raw, 0, $boundary['headerLength']));
        if ($headers === null) {
            return false;
        }
        foreach (self::REQUIRED_HEADERS as $name) {
            if (!isset($headers[$name]) || count($headers[$name]) !== 1) {
                return false;
            }
        }

        $rawBody = substr($raw, $boundary['bodyOffset']);
        return match ($headers['x-xserver-mail-notifier-version'][0]) {
            '1' => $this->authenticateV1($headers, $rawBody),
            '2' => strlen($raw) <= self::MAX_MESSAGE_BYTES
                && $this->authenticateV2($headers, $rawBody),
            default => false,
        };
    }

    /** @param array<string,list<string>> $headers */
    private function authenticateV1(array $headers, string $rawBody): bool
    {
        if (!$this->validUtf8($rawBody)) {
            return false;
        }
        $type = $headers['x-xserver-mail-notifier-type'][0];
        $subject = $headers['subject'][0];
        $date = $headers['date'][0];
        if (!isset(self::V1_SUBJECTS[$type]) || $subject !== self::V1_SUBJECTS[$type]
            || !$this->validV1Date($date) || !$this->validCommonValues($headers)) {
            return false;
        }

        $bodyHash = hash('sha256', $this->normalizeV1Body($rawBody), false);
        return $this->verifyHashAndHmac($headers, [
            '1', $type, $headers['x-xserver-mail-notifier-event'][0], $headers['to'][0],
            $subject, $date, $bodyHash,
        ], $bodyHash);
    }

    /** @param array<string,list<string>> $headers */
    private function authenticateV2(array $headers, string $rawBody): bool
    {
        foreach (self::V2_MIME_HEADERS as $name => $value) {
            if (!isset($headers[$name]) || count($headers[$name]) !== 1 || $headers[$name][0] !== $value) {
                return false;
            }
        }
        $type = $headers['x-xserver-mail-notifier-type'][0];
        $subject = $headers['subject'][0];
        $date = $headers['date'][0];
        if (!isset(self::V2_SUBJECTS[$type]) || !$this->validV2Subject($type, $subject)
            || !$this->validV2Date($date) || !$this->validCommonValues($headers)) {
            return false;
        }

        $flatBase64 = str_replace("\r\n", '', $rawBody);
        $decodedBody = base64_decode($flatBase64, true);
        if (!is_string($decodedBody) || !$this->validV2Body($decodedBody)
            || $this->canonicalBase64($decodedBody) !== $rawBody) {
            return false;
        }
        $bodyHash = hash('sha256', $decodedBody, false);
        return $this->verifyHashAndHmac($headers, [
            '2', $type, $headers['x-xserver-mail-notifier-event'][0], $headers['to'][0],
            $subject, $date,
            self::V2_MIME_HEADERS['mime-version'],
            self::V2_MIME_HEADERS['content-type'],
            self::V2_MIME_HEADERS['content-transfer-encoding'],
            $bodyHash,
        ], $bodyHash);
    }

    /** @param array<string,list<string>> $headers */
    private function validCommonValues(array $headers): bool
    {
        return preg_match('/\A[0-9a-f]{32}\z/D', $headers['x-xserver-mail-notifier-event'][0]) === 1
            && preg_match('/\A[0-9a-f]{64}\z/D', $headers['x-xserver-mail-notifier-body-sha256'][0]) === 1
            && preg_match('/\Ahmac-sha256=[0-9a-f]{64}\z/D', $headers['x-xserver-mail-notifier-auth'][0]) === 1
            && $this->canonicalTo($headers['to'][0]);
    }

    /**
     * @param array<string,list<string>> $headers
     * @param list<string> $fields
     */
    private function verifyHashAndHmac(array $headers, array $fields, string $bodyHash): bool
    {
        $claimedBodyHash = $headers['x-xserver-mail-notifier-body-sha256'][0];
        if (!hash_equals($claimedBodyHash, $bodyHash)) {
            return false;
        }
        $claimedAuth = substr($headers['x-xserver-mail-notifier-auth'][0], strlen('hmac-sha256='));
        return hash_equals($this->hmac($fields), $claimedAuth);
    }

    /** @return array{headerLength:int,bodyOffset:int}|null */
    private function headerBoundary(string $raw): ?array
    {
        $prefix = substr($raw, 0, self::MAX_HEADER_PREFIX_BYTES);
        $limit = strlen($prefix);
        $crlf = strpos($prefix, "\r\n\r\n");
        if ($crlf === false || $crlf + 4 > $limit) {
            return null;
        }
        return ['headerLength' => $crlf, 'bodyOffset' => $crlf + 4];
    }

    /** @return array<string,list<string>>|null */
    private function parseHeaders(string $raw): ?array
    {
        if (!$this->validUtf8($raw)) {
            return null;
        }
        $normalized = str_replace("\r\n", "\n", $raw);
        if (str_contains($normalized, "\r")) {
            return null;
        }
        $headers = [];
        $previousName = null;
        foreach (explode("\n", $normalized) as $line) {
            if ($line === '' || strlen($line) > self::MAX_HEADER_LINE_BYTES) {
                return null;
            }
            if ($line[0] === ' ' || $line[0] === "\t") {
                if ($previousName === null || in_array($previousName, self::REQUIRED_HEADERS, true)) {
                    return null;
                }
                if (isset(self::V2_MIME_HEADERS[$previousName])) {
                    $headers[$previousName][] = "\0folded";
                }
                continue;
            }
            if (preg_match('/\A([!-9;-~]+):(.*)\z/D', $line, $match) !== 1) {
                return null;
            }
            $name = strtolower($match[1]);
            if (in_array($name, self::REQUIRED_HEADERS, true)) {
                if (!str_starts_with($match[2], ' ')) {
                    return null;
                }
                $match[2] = substr($match[2], 1);
            } elseif (isset(self::V2_MIME_HEADERS[$name])) {
                if (!str_starts_with($match[2], ' ')) {
                    $match[2] = "\0invalid-syntax" . $match[2];
                } else {
                    $match[2] = substr($match[2], 1);
                }
            }
            $headers[$name] ??= [];
            $headers[$name][] = $match[2];
            $previousName = $name;
        }
        return $headers;
    }

    private function canonicalTo(string $to): bool
    {
        if ($to === '') {
            return false;
        }
        try {
            $canonical = CanonicalEmail::many(explode(',', $to), false);
        } catch (InvalidArgumentException) {
            return false;
        }
        return implode(',', $canonical) === $to;
    }

    private function validV1Date(string $date): bool
    {
        return $this->validDateWithOffset($date, '+0000');
    }

    private function validV2Date(string $date): bool
    {
        return $this->validDateWithOffset($date, '+0900');
    }

    private function validDateWithOffset(string $date, string $offset): bool
    {
        if (preg_match('/[\x00-\x1f\x7f]/', $date) === 1) {
            return false;
        }
        $parsed = DateTimeImmutable::createFromFormat(
            '!D, d M Y H:i:s O',
            $date,
            new DateTimeZone('UTC'),
        );
        return $parsed instanceof DateTimeImmutable && $parsed->format('D, d M Y H:i:s O') === $date
            && str_ends_with($date, ' ' . $offset);
    }

    private function validUtf8(string $value): bool
    {
        return preg_match('//u', $value) === 1;
    }

    private function validV2Body(string $body): bool
    {
        return $this->validUtf8($body) && !str_contains($body, "\0") && !str_contains($body, "\r")
            && str_ends_with($body, "\n") && !str_ends_with($body, "\n\n");
    }

    private function normalizeV1Body(string $body): string
    {
        return str_replace("\r", "\n", str_replace("\r\n", "\n", $body));
    }

    private function encodeSubject(string $subject): string
    {
        $points = preg_split('//u', $subject, -1, PREG_SPLIT_NO_EMPTY);
        if (!is_array($points)) {
            throw new InvalidArgumentException('Invalid system mail input');
        }
        $chunks = [];
        $chunk = '';
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
        return implode(' ', array_map(
            static fn (string $value): string => '=?UTF-8?B?' . base64_encode($value) . '?=',
            $chunks,
        ));
    }

    private function validV2Subject(string $type, string $subject): bool
    {
        return $subject === $this->encodeSubject(self::V2_SUBJECTS[$type]['real'])
            || $subject === $this->encodeSubject(self::V2_SUBJECTS[$type]['test']);
    }

    private function canonicalBase64(string $body): string
    {
        return rtrim(chunk_split(base64_encode($body), 76, "\r\n"), "\r\n") . "\r\n";
    }

    /** @param list<string> $fields */
    private function hmac(array $fields): string
    {
        $framed = '';
        foreach ($fields as $field) {
            $framed .= strlen($field) . ':' . $field . "\n";
        }
        return hash_hmac('sha256', $framed, $this->key, false);
    }
}
