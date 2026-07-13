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
    private const SUBJECTS = [
        'error' => 'Xserver mail notifier error',
        'recovery' => 'Xserver mail notifier recovered',
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

    public function __construct(private readonly string $key)
    {
        if (strlen($key) !== 32) {
            throw new InvalidArgumentException('Invalid system mail key');
        }
    }

    /** @param list<string> $recipients */
    public function build(string $type, array $recipients, string $date, string $eventId, string $body): string
    {
        try {
            $to = implode(',', CanonicalEmail::many($recipients, false));
        } catch (InvalidArgumentException) {
            throw new InvalidArgumentException('Invalid system mail input');
        }
        if (!isset(self::SUBJECTS[$type])
            || preg_match('/\A[0-9a-f]{32}\z/D', $eventId) !== 1
            || !$this->validDate($date)
            || !$this->validUtf8($body)) {
            throw new InvalidArgumentException('Invalid system mail input');
        }

        $normalizedBody = $this->normalizeBody($body);
        $bodyHash = hash('sha256', $normalizedBody, false);
        $subject = self::SUBJECTS[$type];
        $hmac = $this->hmac(['1', $type, $eventId, $to, $subject, $date, $bodyHash]);
        $lines = [
            'To: ' . $to,
            'Subject: ' . $subject,
            'Date: ' . $date,
            'X-Xserver-Mail-Notifier-Version: 1',
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
        $wire = implode("\r\n", $lines) . "\r\n\r\n"
            . str_replace("\n", "\r\n", $normalizedBody);
        if (strlen($wire) > self::MAX_MESSAGE_BYTES) {
            throw new InvalidArgumentException('Invalid system mail input');
        }
        return $wire;
    }

    public function isAuthentic(string $raw): bool
    {
        $boundary = $this->headerBoundary($raw);
        if ($boundary === null || !$this->validUtf8(substr($raw, $boundary['bodyOffset']))) {
            return false;
        }
        $headerBytes = substr($raw, 0, $boundary['headerLength']);
        $headers = $this->parseHeaders($headerBytes);
        if ($headers === null) {
            return false;
        }
        foreach (self::REQUIRED_HEADERS as $name) {
            if (!isset($headers[$name]) || count($headers[$name]) !== 1) {
                return false;
            }
        }

        $version = $headers['x-xserver-mail-notifier-version'][0];
        $type = $headers['x-xserver-mail-notifier-type'][0];
        $event = $headers['x-xserver-mail-notifier-event'][0];
        $to = $headers['to'][0];
        $subject = $headers['subject'][0];
        $date = $headers['date'][0];
        $claimedBodyHash = $headers['x-xserver-mail-notifier-body-sha256'][0];
        $claimedAuth = $headers['x-xserver-mail-notifier-auth'][0];
        if ($version !== '1' || !isset(self::SUBJECTS[$type]) || $subject !== self::SUBJECTS[$type]
            || preg_match('/\A[0-9a-f]{32}\z/D', $event) !== 1
            || preg_match('/\A[0-9a-f]{64}\z/D', $claimedBodyHash) !== 1
            || preg_match('/\Ahmac-sha256=([0-9a-f]{64})\z/D', $claimedAuth, $match) !== 1
            || !$this->validDate($date) || !$this->canonicalTo($to)) {
            return false;
        }
        $bodyHash = hash('sha256', $this->normalizeBody(substr($raw, $boundary['bodyOffset'])), false);
        if (!hash_equals($claimedBodyHash, $bodyHash)) {
            return false;
        }
        $expected = $this->hmac([$version, $type, $event, $to, $subject, $date, $bodyHash]);
        return hash_equals($expected, $match[1]);
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

    private function validDate(string $date): bool
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
            && str_ends_with($date, ' +0000');
    }

    private function validUtf8(string $value): bool
    {
        return preg_match('//u', $value) === 1;
    }

    private function normalizeBody(string $body): string
    {
        return str_replace("\r", "\n", str_replace("\r\n", "\n", $body));
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
