<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeZone;

final class NotificationFormatter
{
    private const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];

    public function title(MailMessage $message): string
    {
        $from = self::withoutTitleControls($message->from);
        $subject = self::withoutTitleControls($message->subject);
        $title = ($from === '' ? '（差出人不明）' : $from)
            . '：'
            . ($subject === '' ? '（件名なし）' : $subject);

        return self::firstCodePoints($title, 100);
    }

    public function format(MailMessage $message): string
    {
        $receivedAt = $message->receivedAt->setTimezone(new DateTimeZone('Asia/Tokyo'));
        $date = $receivedAt->format('Y年m月d日（')
            . self::WEEKDAYS[(int) $receivedAt->format('w')]
            . $receivedAt->format('）H時i分s秒');

        $from = self::withoutControlCharacters($message->from);
        $to = self::withoutControlCharacters($message->to);
        $cc = self::withoutControlCharacters($message->cc);
        $bcc = self::withoutControlCharacters($message->bcc);

        $lines = [
            '受信日時：' . $date,
            'From：' . $from,
            'To：' . $to,
        ];

        if ($cc !== '') {
            $lines[] = 'Cc：' . $cc;
        }
        if ($bcc !== '') {
            $lines[] = 'Bcc：' . $bcc;
        }

        if ($message->attachments !== []) {
            $lines[] = '添付ファイル：あり（' . count($message->attachments) . '件）';
            foreach (array_slice($message->attachments, 0, 20) as $attachment) {
                $lines[] = '・' . $attachment->filename . '（' . self::formatSize($attachment->sizeBytes) . '）';
            }
            if (count($message->attachments) > 20) {
                $lines[] = '・ほか' . (count($message->attachments) - 20) . '件';
            }
            $lines[] = '';
            $lines[] = '※添付ファイルはメールボックスで確認してください。';
        }

        $lines[] = '件名：' . self::withoutControlCharacters($message->subject);
        $lines[] = '本文：';
        $lines[] = self::withoutControlCharacters($message->notificationBody);

        return implode("\n", $lines);
    }

    private static function formatSize(int $bytes): string
    {
        if ($bytes < 1024) {
            return $bytes . ' B';
        }
        if ($bytes < 1024 * 1024) {
            return round($bytes / 1024) . ' KB';
        }

        return number_format($bytes / (1024 * 1024), 1, '.', '') . ' MB';
    }

    private static function withoutControlCharacters(string $value): string
    {
        $clean = '';
        $length = strlen($value);

        for ($offset = 0; $offset < $length;) {
            $byte = ord($value[$offset]);
            if (($byte < 0x20 && !in_array($byte, [0x09, 0x0A, 0x0D], true)) || $byte === 0x7F) {
                ++$offset;
                continue;
            }
            if ($byte < 0x80) {
                $clean .= $value[$offset++];
                continue;
            }

            $sequenceLength = self::utf8SequenceLength($value, $offset);
            if ($sequenceLength === 0) {
                $clean .= "\xEF\xBF\xBD";
                ++$offset;
                continue;
            }

            // U+0080..U+009F (C1 controls) are encoded as C2 80..9F.
            if ($byte === 0xC2 && ord($value[$offset + 1]) <= 0x9F) {
                $offset += 2;
                continue;
            }

            $clean .= substr($value, $offset, $sequenceLength);
            $offset += $sequenceLength;
        }

        return $clean;
    }

    private static function withoutTitleControls(string $value): string
    {
        return trim(str_replace(["\t", "\n", "\r"], '', self::withoutControlCharacters($value)));
    }

    private static function firstCodePoints(string $value, int $limit): string
    {
        $offset = 0;
        $length = strlen($value);
        for ($count = 0; $offset < $length && $count < $limit; ++$count) {
            $sequenceLength = self::utf8SequenceLength($value, $offset);
            $offset += $sequenceLength === 0 ? 1 : $sequenceLength;
        }

        return substr($value, 0, $offset);
    }

    private static function utf8SequenceLength(string $value, int $offset): int
    {
        $length = strlen($value);
        $first = ord($value[$offset]);
        $continuation = static fn (int $index): bool => $index < $length
            && (ord($value[$index]) & 0xC0) === 0x80;

        if ($first >= 0xC2 && $first <= 0xDF && $continuation($offset + 1)) {
            return 2;
        }
        if ($offset + 2 < $length && $continuation($offset + 2)) {
            $second = ord($value[$offset + 1]);
            if (($first === 0xE0 && $second >= 0xA0 && $second <= 0xBF)
                || (($first >= 0xE1 && $first <= 0xEC) && $continuation($offset + 1))
                || ($first === 0xED && $second >= 0x80 && $second <= 0x9F)
                || (($first >= 0xEE && $first <= 0xEF) && $continuation($offset + 1))) {
                return 3;
            }
        }
        if ($offset + 3 < $length && $continuation($offset + 2) && $continuation($offset + 3)) {
            $second = ord($value[$offset + 1]);
            if (($first === 0xF0 && $second >= 0x90 && $second <= 0xBF)
                || (($first >= 0xF1 && $first <= 0xF3) && $continuation($offset + 1))
                || ($first === 0xF4 && $second >= 0x80 && $second <= 0x8F)) {
                return 4;
            }
        }

        return 0;
    }
}
