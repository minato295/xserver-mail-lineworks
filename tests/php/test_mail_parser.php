<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';

use XserverMail\MailMessage;
use XserverMail\MailParser;
use XserverMail\NotificationFormatter;
use XserverMail\AttachmentMetadata;
use XserverMail\QuoteTrimmer;

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function fixture(string $name): string
{
    $contents = file_get_contents(dirname(__DIR__) . '/fixtures/' . $name);
    if ($contents === false) {
        throw new RuntimeException('Fixture could not be read: ' . $name);
    }

    return $contents;
}

$fallback = new DateTimeImmutable('2026-07-11T00:00:00+09:00');
$parser = new MailParser();

$recipientRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: Employee <employee@example.invalid>',
    'To: Team: first@example.invalid, Second <second@example.invalid>;',
    'Cc: =?UTF-8?B?5pel5pys6Kqe?= <INFO@EXAMPLE.INVALID>,',
    ' folded@example.invalid',
    'Bcc: hidden@example.invalid',
    'Reply-To: reply@example.invalid',
    'Delivered-To: delivered@example.invalid',
    'Subject: recipients',
    '',
    'Body mentions body@example.invalid',
]);
$recipients = $parser->parse($recipientRaw, $fallback)->visibleRecipientAddresses;
check($recipients === [
    'employee@example.invalid', 'first@example.invalid',
    'second@example.invalid', 'INFO@EXAMPLE.INVALID', 'folded@example.invalid',
], 'Only every structured To/Cc address must be retained in header order');
check(!in_array('hidden@example.invalid', $recipients, true), 'Bcc must not be a visible recipient');
check(!in_array('reply@example.invalid', $recipients, true), 'Reply-To must not be a visible recipient');

check(QuoteTrimmer::plainText("新規\n> 引用\n続き") === "新規\n続き", 'Inline quote lines must be removed without hiding later new text');
check(QuoteTrimmer::plainText("新規\nOn Mon, Jul 13, 2026 at 10:00 AM Sender <sender@example.invalid> wrote:\n過去本文") === '新規', 'Strict English reply boundary must hide the following quote');
check(QuoteTrimmer::plainText("新規\n2026年7月13日(月) 10:00 送信者 <sender@example.invalid>:\n過去本文") === '新規', 'Dated Japanese reply boundary must hide the following quote');
check(QuoteTrimmer::plainText("新規\n-----Original Message-----\nFrom: old@example.invalid\nOld body") === '新規', 'Original Message boundary must hide the following quote');
check(QuoteTrimmer::plainText("新規\n差出人: old@example.invalid\n送信日時: 2026年7月12日 9:00\n宛先: receiver@example.invalid\n件名: 過去\n過去本文") === '新規', 'Qualified Outlook header block must hide the following quote');
check(QuoteTrimmer::plainText("新規\n以下のメッセージを引用\n過去本文") === '新規', 'Explicit Japanese quote label must hide the following quote');
check(QuoteTrimmer::plainText("通常文で wrote: と説明") === "通常文で wrote: と説明", 'Ordinary wrote prose must remain');
check(QuoteTrimmer::plainText("差出人: 説明文\n通常本文") === "差出人: 説明文\n通常本文", 'An isolated sender label must remain');
check(QuoteTrimmer::plainText("From: explanation\n件名: explanation\n通常本文") === "From: explanation\n件名: explanation\n通常本文", 'Isolated From and subject labels must remain');
check(QuoteTrimmer::plainText("差出人: old@example.invalid\n送信日時: 2026年7月12日 9:00\n通常本文") === "差出人: old@example.invalid\n送信日時: 2026年7月12日 9:00\n通常本文", 'Incomplete Outlook headers must remain');
check(str_contains(QuoteTrimmer::html('<div class="not_gmail_quote">残す</div>'), '残す'), 'gmail_quote class matching must use exact tokens');
check(str_contains(QuoteTrimmer::html('<div data-example="<blockquote>">属性の外側は残す</div>'), '属性の外側は残す'), 'Tag-like text in a quoted attribute must not start quote removal');
check(!str_contains(QuoteTrimmer::html('<div>新規</div><blockquote>引用</blockquote><div>続き</div>'), '引用'), 'HTML blockquote must be removed');
check(!str_contains(QuoteTrimmer::html('<div>新規</div><div class="foo GMAIL_QUOTE bar">引用</div><div>続き</div>'), '引用'), 'Whitespace-delimited gmail_quote class tokens must be removed case-insensitively');

$measureMalformedQuote = static function (int $repetitions): array {
    $html = '<blockquote>' . str_repeat('<a "', $repetitions) . '</blockquote><div>KEEP_AFTER_MALFORMED_QUOTE</div>';
    $fastestNanos = PHP_INT_MAX;
    $trimmed = '';
    for ($attempt = 0; $attempt < 3; ++$attempt) {
        $started = hrtime(true);
        $trimmed = QuoteTrimmer::html($html);
        $fastestNanos = min($fastestNanos, hrtime(true) - $started);
    }

    return [$trimmed, $fastestNanos];
};
[$malformedSmallResult, $malformedSmallNanos] = $measureMalformedQuote(20_000);
[$malformedLargeResult, $malformedLargeNanos] = $measureMalformedQuote(40_000);
check(
    str_contains($malformedSmallResult, 'KEEP_AFTER_MALFORMED_QUOTE')
        && str_contains($malformedLargeResult, 'KEEP_AFTER_MALFORMED_QUOTE')
        && $malformedLargeNanos <= max(1, $malformedSmallNanos) * 3.5,
    'Malformed quote HTML must retain following text with approximately linear scaling',
);

$rawTextHtmlRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Raw text tag decoy',
    'MIME-Version: 1.0',
    'Content-Type: text/html; charset=UTF-8',
    '',
    '<script>"<blockquote>"</script><div>KEEP_NEW_TEXT</div>',
]);
$rawTextHtml = $parser->parse($rawTextHtmlRaw, $fallback);
check($rawTextHtml->notificationBody === 'KEEP_NEW_TEXT', 'Tag-like text inside script raw text must not hide following notification text');

foreach (['title', 'textarea'] as $rcdataElement) {
    $rcdataHtmlRaw = implode("\r\n", [
        'From: sender@example.invalid',
        'To: receiver@example.invalid',
        'Subject: RCDATA tag decoy',
        'MIME-Version: 1.0',
        'Content-Type: text/html; charset=UTF-8',
        '',
        '<' . $rcdataElement . '>"<blockquote>"</' . $rcdataElement . '><div>KEEP_AFTER_' . strtoupper($rcdataElement) . '</div>',
    ]);
    $rcdataHtml = $parser->parse($rcdataHtmlRaw, $fallback);
    check(
        str_contains($rcdataHtml->notificationBody, 'KEEP_AFTER_' . strtoupper($rcdataElement)),
        'Tag-like text inside ' . $rcdataElement . ' RCDATA must not hide following notification text',
    );
}

$opaqueRawTextFailures = [];
foreach (['xmp', 'iframe', 'noembed', 'noframes', 'noscript'] as $rawTextElement) {
    $rawTextElementHtml = '<' . $rawTextElement . '>"<blockquote>"</' . $rawTextElement
        . '><div>KEEP_AFTER_' . strtoupper($rawTextElement) . '</div>';
    $trimmedRawTextElementHtml = QuoteTrimmer::html($rawTextElementHtml);
    if (!str_contains($trimmedRawTextElementHtml, 'KEEP_AFTER_' . strtoupper($rawTextElement))) {
        $opaqueRawTextFailures[] = $rawTextElement;
    }
}
$plaintextHtml = '<plaintext>"<blockquote>"<div>KEEP_INSIDE_PLAINTEXT_TO_EOF</div>';
$trimmedPlaintextHtml = QuoteTrimmer::html($plaintextHtml);
check(
    str_contains($trimmedPlaintextHtml, '<blockquote>')
        && str_contains($trimmedPlaintextHtml, 'KEEP_INSIDE_PLAINTEXT_TO_EOF'),
    'plaintext content must remain opaque through EOF rather than creating later HTML body content',
);
check(
    $opaqueRawTextFailures === [],
    'Tag-like quote text must stay opaque in every raw-text element: ' . implode(', ', $opaqueRawTextFailures),
);

check(QuoteTrimmer::plainText("On project wrote:\nkeep this") === "On project wrote:\nkeep this", 'On prose without reply date and sender structure must remain in full');

$plain = $parser->parse(fixture('plain.eml'), $fallback);
check($plain->subject === 'お問い合わせ', 'Japanese MIME subject must be decoded');
check($plain->from === '送信者 <sender@example.invalid>', 'Japanese display name must be decoded');
check($plain->to === 'First <first@example.invalid>, Second <second@example.invalid>', 'All To addresses must be retained');
check($plain->cc === 'Manager <manager@example.invalid>, Support <support@example.invalid>', 'All Cc addresses must be retained');
check($plain->bcc === 'Archive <archive@example.invalid>', 'Bcc must be retained when present');
check($plain->receivedAt->getTimezone()->getName() === 'Asia/Tokyo', 'Date must use the Asia/Tokyo timezone');
check($plain->receivedAt->format('Y-m-d H:i:s') === '2026-07-11 16:05:09', 'Date must be converted to Japan time');
check($plain->body === "本文です。\n2行目です。", 'Plain body must be normalized without trailing line breaks');
check($plain->notificationBody === $plain->body, 'Unquoted plain notification body must retain the full body');
check($plain->messageIdHash === hash('sha256', 'plain-fixture@example.invalid'), 'Decoded Message-ID must be SHA-256 hashed');

$html = $parser->parse(fixture('html.eml'), $fallback);
check($html->body === "HTML本文&確認\n2行目です。", 'HTML-only body must become plain text with visual line breaks');
check($html->notificationBody === $html->body, 'Unquoted HTML notification body must retain the full body');
check(!str_contains($html->body, '<script'), 'HTML tags must not survive fallback conversion');
check(!str_contains($html->body, 'SCRIPT_SECRET'), 'Script content must not survive fallback conversion');
check($html->cc === '', 'Absent Cc must be represented as an empty string');
check($html->bcc === '', 'Absent Bcc must be represented as an empty string');

$obfuscatedHtmlRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Obfuscated active content',
    'MIME-Version: 1.0',
    'Content-Type: text/html; charset=UTF-8',
    'Content-Transfer-Encoding: 8bit',
    '',
    '<p>Before</p><ScRiPt data-x=">">SCRIPT_QUOTED_SECRET</sCrIpT><STYLE media=\'a>b\'>STYLE_QUOTED_SECRET</StYlE><script data-x="></script>">SCRIPT_FAKE_CLOSE_SECRET</script><div>After</div>',
]);
$obfuscatedHtml = $parser->parse($obfuscatedHtmlRaw, $fallback);
check(!str_contains($obfuscatedHtml->body, 'SCRIPT_QUOTED_SECRET'), 'Script content behind a quoted > attribute must be removed');
check(!str_contains($obfuscatedHtml->body, 'STYLE_QUOTED_SECRET'), 'Style content behind a quoted > attribute must be removed');
check(!str_contains($obfuscatedHtml->body, 'SCRIPT_FAKE_CLOSE_SECRET'), 'A closing-tag decoy inside a quoted attribute must not terminate script removal');
check(str_contains($obfuscatedHtml->body, 'Before') && str_contains($obfuscatedHtml->body, 'After'), 'Text outside active-content elements must be retained');

$quotedHtmlRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Quoted HTML',
    'MIME-Version: 1.0',
    'Content-Type: text/html; charset=UTF-8',
    '',
    '<div>New text</div><blockquote><div>Old blockquote</div></blockquote><div class="prefix gmail_quote suffix">Old Gmail quote</div><div class="not_gmail_quote">Keep this</div>',
]);
$quotedHtml = $parser->parse($quotedHtmlRaw, $fallback);
check(str_contains($quotedHtml->body, 'Old blockquote') && str_contains($quotedHtml->body, 'Old Gmail quote'), 'Full HTML-derived body must preserve quoted history');
check($quotedHtml->notificationBody === "New text\nKeep this", 'HTML notification body must remove only recognized quoted elements');

$quoteOnly = $parser->parse("From: sender@example.invalid\r\nTo: receiver@example.invalid\r\n\r\n> quoted only", $fallback);
check($quoteOnly->body === '> quoted only', 'Full plain body must preserve a quote-only message');
check($quoteOnly->notificationBody === '（引用部分は省略しました）', 'Quote-only notification body must use the fixed fallback');

$multipart = $parser->parse(fixture('multipart.eml'), $fallback);
check($multipart->body === 'プレーン本文', 'Plain text must be preferred over HTML');
check(!str_contains($multipart->body, 'ATTACHMENT_SECRET'), 'Attachments must not be included');

$attachmentRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Attachments',
    'MIME-Version: 1.0',
    'Content-Type: multipart/mixed; boundary="outer"',
    '',
    '--outer',
    'Content-Type: multipart/alternative; boundary="alternative"',
    '',
    '--alternative',
    'Content-Type: text/plain; charset=UTF-8',
    '',
    'Body',
    '--alternative',
    'Content-Type: text/html; charset=UTF-8',
    '',
    '<p>Body</p>',
    '--alternative--',
    '--outer',
    'Content-Type: image/png; name="inline.png"',
    'Content-Disposition: inline; filename="inline.png"',
    'Content-Transfer-Encoding: base64',
    '',
    base64_encode('inline image'),
    '--outer',
    'Content-Type: application/pdf',
    'Content-Disposition: attachment; filename="estimate.pdf"',
    'Content-Transfer-Encoding: base64',
    '',
    base64_encode(str_repeat('x', 123 * 1024)),
    '--outer--',
]);
$withAttachment = $parser->parse($attachmentRaw, $fallback);
check(count($withAttachment->attachments) === 1, 'Only MIME attachment disposition parts must be counted');
check($withAttachment->attachments[0]->filename === 'estimate.pdf', 'Plain attachment filename must be retained');
check($withAttachment->attachments[0]->sizeBytes === 123 * 1024, 'Attachment size must use decoded bytes');

$encodedAttachmentRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Encoded attachment names',
    'MIME-Version: 1.0',
    'Content-Type: multipart/mixed; boundary="encoded"',
    '',
    '--encoded',
    'Content-Type: text/plain; charset=UTF-8',
    '',
    'Body',
    '--encoded',
    'Content-Type: application/octet-stream',
    'Content-Disposition: attachment; filename="=?UTF-8?B?5YaZ55yfLmpwZw==?="',
    '',
    'abc',
    '--encoded',
    'Content-Type: application/pdf',
    "Content-Disposition: attachment; filename*=UTF-8''%E8%A6%8B%E7%A9%8D%E6%9B%B8.pdf",
    '',
    'abcd',
    '--encoded',
    'Content-Type: application/octet-stream',
    'Content-Disposition: attachment',
    '',
    'x',
    '--encoded--',
]);
$encodedAttachments = $parser->parse($encodedAttachmentRaw, $fallback)->attachments;
check(array_map(static fn (AttachmentMetadata $attachment): string => $attachment->filename, $encodedAttachments) === ['写真.jpg', '見積書.pdf', '名称なし'], 'Encoded-word, RFC 2231, and missing filenames must be normalized');

$multipartAttachmentRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Multipart attachment',
    'MIME-Version: 1.0',
    'Content-Type: multipart/mixed; boundary="special"',
    '',
    '--special',
    'Content-Type: text/plain; charset=UTF-8',
    '',
    'Body',
    '--special',
    'Content-Type: multipart/mixed; boundary="attached-message"',
    'Content-Disposition: attachment; filename="forwarded.mime"',
    '',
    '--attached-message',
    'Content-Type: text/plain; charset=UTF-8',
    '',
    'Forwarded body',
    '--attached-message--',
    '--special--',
]);
$multipartAttachments = $parser->parse($multipartAttachmentRaw, $fallback)->attachments;
check(array_map(static fn (AttachmentMetadata $attachment): string => $attachment->filename, $multipartAttachments) === ['forwarded.mime'], 'Every explicitly attached multipart part must be counted');

$signedAttachmentRaw = implode("\r\n", [
    'From: sender@example.invalid',
    'To: receiver@example.invalid',
    'Subject: Signature attachment',
    'MIME-Version: 1.0',
    'Content-Type: multipart/signed; protocol="application/pgp-signature"; boundary="signed"',
    '',
    '--signed',
    'Content-Type: text/plain; charset=UTF-8',
    '',
    'Signed body',
    '--signed',
    'Content-Type: application/pgp-signature; name="signature.asc"',
    'Content-Disposition: attachment; filename="signature.asc"',
    '',
    'signed bytes',
    '--signed--',
]);
$signatureAttachments = $parser->parse($signedAttachmentRaw, $fallback)->attachments;
check(array_map(static fn (AttachmentMetadata $attachment): string => $attachment->filename, $signatureAttachments) === ['signature.asc'], 'An explicitly attached signature child of multipart/signed must be counted');

$noAttachment = $parser->parse("From: sender@example.invalid\r\nTo: receiver@example.invalid\r\n\r\nBody", $fallback);
check($noAttachment->attachments === [], 'Messages without attachments must expose an empty immutable metadata array');

$unsafeName = new AttachmentMetadata(str_repeat('あ', 101) . "\r\n\x00hidden", 1);
check(preg_match_all('/./u', $unsafeName->filename) === 100, 'Attachment display names must be limited to 100 Unicode code points');
check(!preg_match('/\p{Cc}/u', $unsafeName->filename), 'Attachment display names must not contain line breaks or control characters');

$missingDateRaw = "From: sender@example.invalid\r\nTo: receiver@example.invalid\r\nSubject: No date\r\n\r\nBody";
$missingDate = $parser->parse($missingDateRaw, $fallback);
check($missingDate->receivedAt === $fallback, 'Missing Date must use the exact fallback instance');

$invalidDateRaw = "Date: definitely-not-a-date\r\nFrom: sender@example.invalid\r\nTo: receiver@example.invalid\r\nSubject: Bad date\r\n\r\nBody";
$invalidDate = $parser->parse($invalidDateRaw, $fallback);
check($invalidDate->receivedAt === $fallback, 'Invalid Date must use the exact fallback instance');

$oversized = str_repeat('x', (10 * 1024 * 1024) + 1);
try {
    $parser->parse($oversized, $fallback);
    throw new RuntimeException('Input larger than 10 MiB must be rejected');
} catch (InvalidArgumentException $exception) {
    check($exception->getMessage() === 'Email exceeds the 10 MiB limit', 'Oversized input must use a safe error message');
}

$formatter = new NotificationFormatter();
check($formatter->title($plain) === '送信者 <sender@example.invalid>：お問い合わせ', 'Title must contain the sanitized sender and subject');
$emptyTitleMessage = new MailMessage($fallback, "\x00\n", 'to', '', '', "\x07\r", 'body', 'body', hash('sha256', ''));
check($formatter->title($emptyTitleMessage) === '（差出人不明）：（件名なし）', 'Title must use fixed fallbacks after sanitization');
$whitespaceTitleMessage = new MailMessage($fallback, '   ', 'to', '', '', " \t ", 'body', 'body', hash('sha256', ''));
check($formatter->title($whitespaceTitleMessage) === '（差出人不明）：（件名なし）', 'Whitespace-only title fields must use fixed fallbacks');
$controlTitleMessage = new MailMessage($fallback, "送信\n者\x00", 'to', '', '', "件\t名\x7F", 'body', 'body', hash('sha256', ''));
check($formatter->title($controlTitleMessage) === '送信者：件名', 'Title must remove line breaks and every control character');
$longTitleMessage = new MailMessage($fallback, str_repeat('あ', 99), 'to', '', '', '終端', 'body', 'body', hash('sha256', ''));
$longTitle = $formatter->title($longTitleMessage);
check(preg_match_all('/./u', $longTitle) === 100, 'Title must be limited to exactly 100 Unicode code points');
check(preg_match('//u', $longTitle) === 1 && str_ends_with($longTitle, '：'), 'Title truncation must never split a UTF-8 byte sequence');

$formatted = $formatter->format($plain);
$expected = implode("\n", [
    '受信日時：2026年07月11日（土）16時05分09秒',
    'From：送信者 <sender@example.invalid>',
    'To：First <first@example.invalid>, Second <second@example.invalid>',
    'Cc：Manager <manager@example.invalid>, Support <support@example.invalid>',
    'Bcc：Archive <archive@example.invalid>',
    '件名：お問い合わせ',
    '本文：',
    '本文です。',
    '2行目です。',
]);
check($formatted === $expected, 'Formatter must use the required order and Japanese date format');

$withoutOptional = new MailMessage(
    $fallback,
    "sender\x00@example.invalid",
    "receiver@example.invalid\x07",
    '',
    '',
    "Subject\x1F",
    "line 1\nline\t2\x00",
    "line 1\nline\t2\x00",
    hash('sha256', ''),
);
$safe = (new NotificationFormatter())->format($withoutOptional);
check(!str_contains($safe, 'Cc：'), 'Empty Cc line must be omitted');
check(!str_contains($safe, 'Bcc：'), 'Empty Bcc line must be omitted');
check(!preg_match('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/', $safe), 'Unsafe control characters must be removed');
check(str_contains($safe, "line 1\nline\t2"), 'Newlines and tabs must be preserved');

$attachmentMessage = new MailMessage(
    $fallback,
    'sender@example.invalid',
    'receiver@example.invalid',
    '',
    'bcc@example.invalid',
    'Subject',
    'Body',
    'Body',
    hash('sha256', ''),
    [
        new AttachmentMetadata('small.bin', 1023),
        new AttachmentMetadata('one-kib.bin', 1024),
        new AttachmentMetadata('one-mib.bin', 1024 * 1024),
        new AttachmentMetadata('photo.jpg', 2_516_582),
    ],
);
$attachmentFormatted = (new NotificationFormatter())->format($attachmentMessage);
check(str_contains($attachmentFormatted, "添付ファイル：あり（4件）\n・small.bin（1023 B）\n・one-kib.bin（1 KB）\n・one-mib.bin（1.0 MB）\n・photo.jpg（2.4 MB）\n\n※添付ファイルはメールボックスで確認してください。"), 'Attachment block and B/KB/MB boundary sizes must use the required format');
$orderedLabels = array_map(static fn (string $label): int|false => strpos($attachmentFormatted, $label), ['Bcc：', '添付ファイル：', '件名：', '本文：']);
check(!in_array(false, $orderedLabels, true), 'Every ordered notification label must exist');
check($orderedLabels[0] < $orderedLabels[1] && $orderedLabels[1] < $orderedLabels[2] && $orderedLabels[2] < $orderedLabels[3], 'Attachment block must appear after Bcc and before subject');

$separateBodyMessage = new MailMessage($fallback, 'from', 'to', '', '', 'subject', 'Full body with quoted history', 'Notification preview', hash('sha256', ''));
$separateBodyFormatted = $formatter->format($separateBodyMessage);
check(str_contains($separateBodyFormatted, "本文：\nNotification preview"), 'Formatter must use notificationBody');
check(!str_contains($separateBodyFormatted, 'Full body with quoted history'), 'Formatter must not expose the retained full body');

$manyAttachments = [];
for ($index = 1; $index <= 23; ++$index) {
    $manyAttachments[] = new AttachmentMetadata('file-' . $index, $index);
}
$limitedMessage = new MailMessage($fallback, 'from', 'to', '', '', 'subject', 'body', 'body', hash('sha256', ''), $manyAttachments);
$limited = (new NotificationFormatter())->format($limitedMessage);
check(substr_count($limited, "\n・file-") === 20, 'At most 20 attachment detail lines must be displayed');
check(str_contains($limited, "\n・ほか3件\n"), 'Attachment overflow count must be displayed');

$controlOnlyOptional = new MailMessage(
    $fallback,
    'sender@example.invalid',
    'receiver@example.invalid',
    "\x00",
    "\x07",
    'Subject',
    'Body',
    'Body',
    hash('sha256', ''),
);
$withoutControlOnlyOptional = (new NotificationFormatter())->format($controlOnlyOptional);
check(!str_contains($withoutControlOnlyOptional, 'Cc：'), 'Cc must be omitted when sanitization makes it empty');
check(!str_contains($withoutControlOnlyOptional, 'Bcc：'), 'Bcc must be omitted when sanitization makes it empty');

$invalidUtf8 = new MailMessage(
    $fallback,
    "送信者\xFF\x00\x01\x7F\xC2\x80@example.invalid",
    'receiver@example.invalid',
    '',
    '',
    "有効な件名\xFE\x1F",
    "有効な本文\n二行目\t\x00\x08\x7F\xC2\x9F\xFF末尾",
    "有効な本文\n二行目\t\x00\x08\x7F\xC2\x9F\xFF末尾",
    hash('sha256', ''),
);
$invalidUtf8Safe = (new NotificationFormatter())->format($invalidUtf8);
check(preg_match('//u', $invalidUtf8Safe) === 1, 'Formatted output must always be valid UTF-8');
check(!preg_match('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]|\xC2[\x80-\x9F]/', $invalidUtf8Safe), 'C0, DEL, and C1 controls must be removed even beside invalid UTF-8');
check(str_contains($invalidUtf8Safe, '送信者') && str_contains($invalidUtf8Safe, "有効な本文\n二行目\t") && str_contains($invalidUtf8Safe, '末尾'), 'Valid text, newlines, and tabs beside invalid UTF-8 must be retained');
check(str_contains($invalidUtf8Safe, "\xEF\xBF\xBD"), 'Invalid UTF-8 bytes must be replaced safely');

fwrite(STDOUT, "PASS: mail parser and formatter\n");
