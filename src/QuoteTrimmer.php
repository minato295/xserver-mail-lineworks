<?php

declare(strict_types=1);

namespace XserverMail;

final class QuoteTrimmer
{
    /**
     * Elements whose content is not tokenized as ordinary HTML until their matching end tag.
     * `noscript` uses RAWTEXT when scripting is enabled; treating it opaquely is the conservative
     * choice for notification trimming because scripts are removed by the later text conversion.
     */
    private const MATCHING_END_OPAQUE_ELEMENTS = [
        'script', // Script data state.
        'style', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript', // RAWTEXT states.
        'title', 'textarea', // RCDATA states.
    ];

    /** `plaintext` switches to text through EOF; a later tag cannot return to normal HTML parsing. */
    private const EOF_OPAQUE_ELEMENT = 'plaintext';

    public static function plainText(string $body): string
    {
        $body = str_replace(["\r\n", "\r"], "\n", $body);
        $lines = explode("\n", $body);
        $kept = [];

        foreach ($lines as $index => $line) {
            if (self::isQuoteBoundary($line) || self::isOutlookBoundary($lines, $index)) {
                break;
            }
            if (preg_match('/^[ \t]*>/', $line) === 1) {
                continue;
            }
            $kept[] = $line;
        }

        $result = implode("\n", $kept);
        $result = preg_replace("/[\t ]+\n/", "\n", $result) ?? $result;
        $result = preg_replace("/\n{3,}/", "\n\n", $result) ?? $result;

        return trim($result);
    }

    public static function html(string $html): string
    {
        $result = '';
        $offset = 0;
        $length = strlen($html);
        $skippedName = null;
        $skippedDepth = 0;

        while ($offset < $length) {
            if ($html[$offset] !== '<') {
                $nextTag = strpos($html, '<', $offset);
                $end = $nextTag === false ? $length : $nextTag;
                if ($skippedName === null) {
                    $result .= substr($html, $offset, $end - $offset);
                }
                $offset = $end;
                continue;
            }

            if (substr($html, $offset, 4) === '<!--') {
                $commentEnd = strpos($html, '-->', $offset + 4);
                $end = $commentEnd === false ? $length : $commentEnd + 3;
                if ($skippedName === null) {
                    $result .= substr($html, $offset, $end - $offset);
                }
                $offset = $end;
                continue;
            }

            $closing = self::closingTag($html, $offset);
            if ($closing !== null) {
                if ($skippedName !== null && $closing['name'] === $skippedName) {
                    --$skippedDepth;
                    if ($skippedDepth === 0) {
                        $skippedName = null;
                    }
                } elseif ($skippedName === null) {
                    $result .= substr($html, $offset, $closing['end'] - $offset);
                }
                $offset = $closing['end'];
                continue;
            }

            $tag = self::openingTag($html, $offset);
            if (!$tag['valid']) {
                if ($skippedName === null) {
                    $result .= substr($html, $offset, $tag['end'] - $offset);
                }
                $offset = $tag['end'];
                continue;
            }

            if ($skippedName !== null) {
                if ($tag['name'] === $skippedName && !$tag['selfClosing']) {
                    ++$skippedDepth;
                }
                $offset = self::opaqueElementEnd($html, $tag);
                continue;
            }

            if (self::isQuotedElement($tag['name'], $tag['attributes'])) {
                if (!$tag['selfClosing']) {
                    $skippedName = $tag['name'];
                    $skippedDepth = 1;
                }
                $offset = $tag['end'];
                continue;
            }

            $end = self::opaqueElementEnd($html, $tag);
            $result .= substr($html, $offset, $end - $offset);
            $offset = $end;
        }

        return $result;
    }

    private static function isQuoteBoundary(string $line): bool
    {
        $trimmed = trim($line);
        if (preg_match(
            '/^On[ \t]+(?=[^\r\n]*(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ \t]+\d{1,2},?[ \t]+\d{4}|\d{1,2}[ \t]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ \t]+\d{4}|\d{4}[-\/]\d{1,2}[-\/]\d{1,2}|\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4}))[^\r\n]*\b\d{1,2}:\d{2}(?:[ \t]*[AP]M)?(?:[ \t]*,)?[ \t]+.+[ \t]+wrote:$/i',
            $trimmed,
        ) === 1) {
            return true;
        }
        if (strcasecmp($trimmed, '-----Original Message-----') === 0) {
            return true;
        }
        if (in_array($trimmed, ['以下のメッセージを引用', '以下のメッセージは引用です'], true)) {
            return true;
        }

        return preg_match('/^\d{4}年\d{1,2}月\d{1,2}日(?:\([^\r\n)]{1,4}\)|（[^\r\n）]{1,4}）)?[ \t]+\d{1,2}:\d{2}(?::\d{2})?[ \t]+\S.+(?:<[^<>\r\n]+>|\S)[ \t]*[:：]$/u', $trimmed) === 1;
    }

    /** @param list<string> $lines */
    private static function isOutlookBoundary(array $lines, int $start): bool
    {
        if (preg_match('/^差出人[ \t]*[：:][ \t]*\S.+$/u', trim($lines[$start])) !== 1) {
            return false;
        }

        $foundSender = false;
        $foundSent = false;
        $foundThirdHeader = false;
        $end = min(count($lines), $start + 6);

        for ($index = $start; $index < $end; ++$index) {
            $line = trim($lines[$index]);
            if (preg_match('/^差出人[ \t]*[：:][ \t]*\S.+$/u', $line) === 1) {
                $foundSender = true;
            } elseif (preg_match('/^送信日時[ \t]*[：:][ \t]*\S.+$/u', $line) === 1) {
                $foundSent = true;
            } elseif (preg_match('/^(?:宛先|件名)[ \t]*[：:][ \t]*\S.+$/u', $line) === 1) {
                $foundThirdHeader = true;
            }
        }

        return $foundSender && $foundSent && $foundThirdHeader;
    }

    /** @return array{valid:bool,name:string,attributes:string,end:int,selfClosing:bool} */
    private static function openingTag(string $html, int $start): array
    {
        $length = strlen($html);
        $nameStart = $start + 1;
        if (($html[$nameStart] ?? '') === '/' || ($html[$nameStart] ?? '') === '!') {
            return self::malformedTag($start, min($length, $start + 1));
        }
        $nameLength = strspn($html, 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789', $nameStart);
        if ($nameLength === 0) {
            return self::malformedTag($start, min($length, $start + 1));
        }
        $boundary = $nameStart + $nameLength;
        if ($boundary < $length && !str_contains(" \t\r\n/>", $html[$boundary])) {
            return self::malformedTag($start, min($length, $start + 1));
        }

        $quote = null;
        $recovery = null;
        for ($tagEnd = $boundary; $tagEnd < $length; ++$tagEnd) {
            $character = $html[$tagEnd];
            if ($quote !== null) {
                if ($character === $quote) {
                    $quote = null;
                } elseif ($character === '<' && $recovery === null) {
                    $recovery = $tagEnd;
                }
                continue;
            }
            if ($character === '"' || $character === "'") {
                $quote = $character;
            } elseif ($character === '<') {
                return self::malformedTag($start, $recovery ?? $tagEnd);
            } elseif ($character === '>') {
                $attributes = substr($html, $boundary, $tagEnd - $boundary);
                return [
                    'valid' => true,
                    'name' => strtolower(substr($html, $nameStart, $nameLength)),
                    'attributes' => $attributes,
                    'end' => $tagEnd + 1,
                    'selfClosing' => preg_match('~/[ \t\r\n]*$~', $attributes) === 1,
                ];
            }
        }

        return self::malformedTag($start, $recovery ?? $length);
    }

    /** @return array{valid:false,name:string,attributes:string,end:int,selfClosing:false} */
    private static function malformedTag(int $start, int $end): array
    {
        return [
            'valid' => false,
            'name' => '',
            'attributes' => '',
            'end' => max($start + 1, $end),
            'selfClosing' => false,
        ];
    }

    /** @return array{name:string,end:int}|null */
    private static function closingTag(string $html, int $start): ?array
    {
        if (substr($html, $start, 2) !== '</') {
            return null;
        }
        $nameStart = $start + 2;
        $nameLength = strspn($html, 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789', $nameStart);
        if ($nameLength === 0) {
            return null;
        }
        $afterName = $nameStart + $nameLength;
        $end = $afterName + strspn($html, " \t\r\n", $afterName);
        if (($html[$end] ?? '') !== '>') {
            return null;
        }

        return ['name' => strtolower(substr($html, $nameStart, $nameLength)), 'end' => $end + 1];
    }

    private static function rawTextElementEnd(string $html, int $offset, string $name): int
    {
        $length = strlen($html);
        while (($candidate = stripos($html, '</' . $name, $offset)) !== false) {
            $closing = self::closingTag($html, $candidate);
            if ($closing !== null && $closing['name'] === $name) {
                return $closing['end'];
            }
            $offset = $candidate + 2 + strlen($name);
        }

        return $length;
    }

    /** @param array{valid:bool,name:string,attributes:string,end:int,selfClosing:bool} $tag */
    private static function opaqueElementEnd(string $html, array $tag): int
    {
        if ($tag['name'] === self::EOF_OPAQUE_ELEMENT) {
            return strlen($html);
        }
        if (in_array($tag['name'], self::MATCHING_END_OPAQUE_ELEMENTS, true)) {
            return self::rawTextElementEnd($html, $tag['end'], $tag['name']);
        }

        return $tag['end'];
    }

    private static function isQuotedElement(string $name, string $attributes): bool
    {
        if ($name === 'blockquote') {
            return true;
        }

        if (preg_match('/(?:^|[ \t\r\n])class[ \t\r\n]*=[ \t\r\n]*(?:"([^"]*)"|\'([^\']*)\'|([^ \t\r\n>]+))/i', $attributes, $match) !== 1) {
            return false;
        }
        $classes = $match[1] !== '' ? $match[1] : ($match[2] !== '' ? $match[2] : ($match[3] ?? ''));
        foreach (preg_split('/[ \t\r\n]+/', trim($classes)) ?: [] as $class) {
            if (strcasecmp($class, 'gmail_quote') === 0) {
                return true;
            }
        }

        return false;
    }

}
