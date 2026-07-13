<?php

declare(strict_types=1);

namespace XserverMail;

final class AttachmentMetadata
{
    public readonly string $filename;

    public function __construct(string $filename, public readonly int $sizeBytes)
    {
        $filename = iconv('UTF-8', 'UTF-8//IGNORE', $filename) ?: '';
        $filename = preg_replace('/\p{Cc}+/u', '', $filename) ?? '';
        $characters = preg_split('//u', $filename, 101, PREG_SPLIT_NO_EMPTY) ?: [];
        $this->filename = implode('', array_slice($characters, 0, 100)) ?: '名称なし';
    }
}
