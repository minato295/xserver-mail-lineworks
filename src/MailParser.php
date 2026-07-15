<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeImmutable;
use DateTimeZone;
use InvalidArgumentException;
use ZBateson\MailMimeParser\Header\AddressHeader;
use ZBateson\MailMimeParser\Header\DateHeader;
use ZBateson\MailMimeParser\MailMimeParser;
use ZBateson\MailMimeParser\Message\PartFilter;

final class MailParser
{
    private const MAX_INPUT_BYTES = 10 * 1024 * 1024;

    public function parse(string $raw, DateTimeImmutable $fallback): MailMessage
    {
        if (strlen($raw) > self::MAX_INPUT_BYTES) {
            throw new InvalidArgumentException('Email exceeds the 10 MiB limit');
        }

        $message = (new MailMimeParser())->parse($raw, false);
        $header = static fn (string $name): string => trim($message->getHeader($name)?->getDecodedValue() ?? '');

        $dateHeader = $message->getHeader('Date');
        $parsedDate = $dateHeader instanceof DateHeader ? $dateHeader->getDateTime() : null;
        $receivedAt = $parsedDate === null
            ? $fallback
            : DateTimeImmutable::createFromMutable($parsedDate)->setTimezone(new DateTimeZone('Asia/Tokyo'));

        $plain = $message->getTextContent(0, 'UTF-8');
        $html = $message->getHtmlContent(0, 'UTF-8');
        $plainAvailable = $plain !== null && trim($plain) !== '';
        $body = $plainAvailable
            ? self::normalizeText($plain)
            : self::htmlToText($html ?? '');
        $notificationBody = $plainAvailable
            ? QuoteTrimmer::plainText($body)
            : self::htmlToText(QuoteTrimmer::html($html ?? ''));
        if (trim($notificationBody) === '') {
            $notificationBody = '（引用部分は省略しました）';
        }

        $attachments = [];
        foreach ($message->getAllParts(PartFilter::fromDisposition('attachment', true, true)) as $part) {
            $stream = $part->getBinaryContentStream();
            $attachments[] = new AttachmentMetadata(
                $part->getFilename() ?? '名称なし',
                $stream === null ? 0 : strlen((string) $stream),
            );
        }

        $visibleRecipientAddresses = [];
        foreach (['To', 'Cc'] as $name) {
            foreach ($message->getAllHeadersByName($name) as $addressHeader) {
                if (!$addressHeader instanceof AddressHeader) {
                    continue;
                }
                foreach ($addressHeader->getAddresses() as $address) {
                    $visibleRecipientAddresses[] = $address->getEmail();
                }
            }
        }

        return new MailMessage(
            $receivedAt,
            $header('From'),
            $header('To'),
            $header('Cc'),
            $header('Bcc'),
            $header('Subject'),
            $body,
            $notificationBody,
            hash('sha256', $header('Message-ID')),
            $attachments,
            $visibleRecipientAddresses,
        );
    }

    private static function htmlToText(string $html): string
    {
        $html = self::withoutActiveContent($html);
        $html = preg_replace('~<(?:br\s*/?|/p|/div|/li|/tr|/h[1-6])\s*>~i', "\n", $html) ?? $html;
        $text = strip_tags($html);
        $text = html_entity_decode($text, ENT_QUOTES | ENT_HTML5, 'UTF-8');

        return self::normalizeText($text);
    }

    private static function withoutActiveContent(string $html): string
    {
        $result = '';
        $offset = 0;
        $length = strlen($html);

        while ($offset < $length) {
            $tagStart = strpos($html, '<', $offset);
            if ($tagStart === false) {
                return $result . substr($html, $offset);
            }

            $result .= substr($html, $offset, $tagStart - $offset);
            $nameStart = $tagStart + 1;
            $nameLength = strspn($html, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", $nameStart);
            $name = strtolower(substr($html, $nameStart, $nameLength));
            $boundary = $nameStart + $nameLength;

            if (!in_array($name, ['script', 'style'], true)
                || ($boundary < $length && !str_contains(" \t\r\n/>", $html[$boundary]))) {
                $result .= '<';
                $offset = $tagStart + 1;
                continue;
            }

            $openingEnd = self::tagEndOutsideQuotes($html, $boundary);
            if ($openingEnd === null) {
                return $result;
            }

            $closingEnd = self::closingTagEnd($html, $openingEnd + 1, $name);
            if ($closingEnd === null) {
                return $result;
            }
            $offset = $closingEnd;
        }

        return $result;
    }

    private static function tagEndOutsideQuotes(string $html, int $offset): ?int
    {
        $quote = null;
        $length = strlen($html);
        for (; $offset < $length; ++$offset) {
            $character = $html[$offset];
            if ($quote !== null) {
                if ($character === $quote) {
                    $quote = null;
                }
            } elseif ($character === '"' || $character === "'") {
                $quote = $character;
            } elseif ($character === '>') {
                return $offset;
            }
        }

        return null;
    }

    private static function closingTagEnd(string $html, int $offset, string $name): ?int
    {
        while (($candidate = stripos($html, '</' . $name, $offset)) !== false) {
            $afterName = $candidate + 2 + strlen($name);
            $end = $afterName + strspn($html, " \t\r\n", $afterName);
            if (($html[$end] ?? null) === '>') {
                return $end + 1;
            }
            $offset = $afterName;
        }

        return null;
    }

    private static function normalizeText(string $text): string
    {
        $text = str_replace(["\r\n", "\r"], "\n", $text);
        $text = preg_replace("/[\t ]+\n/", "\n", $text) ?? $text;
        $text = preg_replace("/\n{3,}/", "\n\n", $text) ?? $text;

        return trim($text);
    }
}
