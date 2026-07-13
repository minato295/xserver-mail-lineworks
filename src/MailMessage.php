<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeImmutable;

final class MailMessage
{
    /** @param list<AttachmentMetadata> $attachments */
    public function __construct(
        public readonly DateTimeImmutable $receivedAt,
        public readonly string $from,
        public readonly string $to,
        public readonly string $cc,
        public readonly string $bcc,
        public readonly string $subject,
        public readonly string $body,
        public readonly string $notificationBody,
        public readonly string $messageIdHash,
        public readonly array $attachments = [],
    ) {
    }
}
