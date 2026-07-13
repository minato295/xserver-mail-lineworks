<?php

declare(strict_types=1);

namespace XserverMail;

use InvalidArgumentException;

final class CanonicalEmail
{
    private const LOCAL = "[A-Za-z0-9!#$%&'*+/=?^_`{|}\\~-]+(?:\\.[A-Za-z0-9!#$%&'*+/=?^_`{|}\\~-]+)*";
    private const LABEL = '[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?';

    public static function one(mixed $value): string
    {
        if (!is_string($value) || substr_count($value, '@') !== 1 || strlen($value) > 254) {
            throw new InvalidArgumentException('Invalid email address');
        }
        [$local, $domain] = explode('@', $value, 2);
        $labels = explode('.', $domain);
        if (strlen($local) > 64 || strlen($domain) > 253 || count($labels) < 2
            || preg_match('~\A' . self::LOCAL . '\z~D', $local) !== 1) {
            throw new InvalidArgumentException('Invalid email address');
        }
        foreach ($labels as $label) {
            if (preg_match('~\A' . self::LABEL . '\z~D', $label) !== 1) {
                throw new InvalidArgumentException('Invalid email address');
            }
        }
        return $local . '@' . strtolower($domain);
    }

    /** @return list<string> */
    public static function many(mixed $value, bool $allowEmpty): array
    {
        if (!is_array($value) || array_is_list($value) === false) {
            throw new InvalidArgumentException('Invalid email address list');
        }
        $canonical = array_map(self::one(...), $value);
        $normalized = array_values(array_unique($canonical, SORT_STRING));
        sort($normalized, SORT_STRING);
        if (!$allowEmpty && $normalized === []) {
            throw new InvalidArgumentException('Invalid email address list');
        }
        return $normalized;
    }
}
