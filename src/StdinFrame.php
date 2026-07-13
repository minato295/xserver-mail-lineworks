<?php

declare(strict_types=1);

namespace XserverMail;

use InvalidArgumentException;

final class StdinFrame
{
    public const MAGIC = "XSERVER-MAIL-FRAME\0\x01";
    public const MAX_CONFIG_BYTES = 65536;
    public const MAX_MESSAGE_BYTES = 10485760;

    /** @return array{configJson:string,message:string} */
    public static function decode($stream): array
    {
        if (!is_resource($stream) || self::readExact($stream, strlen(self::MAGIC)) !== self::MAGIC) {
            throw new InvalidArgumentException('Invalid input frame');
        }
        $length = unpack('Nhigh/Nlow', self::readExact($stream, 8));
        if (!is_array($length) || $length['high'] !== 0
            || $length['low'] < 2 || $length['low'] > self::MAX_CONFIG_BYTES) {
            throw new InvalidArgumentException('Invalid input frame');
        }
        $configJson = self::readExact($stream, $length['low']);
        $message = stream_get_contents($stream, self::MAX_MESSAGE_BYTES + 1);
        if (!is_string($message) || strlen($message) > self::MAX_MESSAGE_BYTES) {
            throw new InvalidArgumentException('Invalid input frame');
        }
        return compact('configJson', 'message');
    }

    private static function readExact($stream, int $length): string
    {
        $value = '';
        while (strlen($value) < $length) {
            $part = fread($stream, $length - strlen($value));
            if (!is_string($part) || $part === '') {
                throw new InvalidArgumentException('Invalid input frame');
            }
            $value .= $part;
        }
        return $value;
    }
}
