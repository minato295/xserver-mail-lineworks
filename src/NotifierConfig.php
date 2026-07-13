<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeImmutable;
use InvalidArgumentException;
use JsonException;

final class NotifierConfig
{
    private const MAX_RECIPIENTS = 32;
    private const MAX_TO_BYTES = 900;
    private const MAX_LOG_PATH_BYTES = 4096;
    private const MAX_HEADER_LINE_BYTES = 997;
    private const MAX_SIGNED_MESSAGE_BYTES = 65535;
    // Upper bounds for all non-configurable header/message bytes. Configurable
    // To and UTF-8 log-path bytes are added explicitly in fromArray().
    private const SYSTEM_MAIL_FIXED_HEADER_LINE_BYTES = 160;
    private const SYSTEM_MAIL_FIXED_MESSAGE_BYTES = 8192;

    private function __construct(
        public readonly string $webhookUrl,
        public readonly array $errorRecipients,
        public readonly string $logPath,
        public readonly string $dedupPath,
        public readonly array $notificationPinnedTargets,
        public readonly array $notificationTargets,
        public readonly string $systemMailHmacKey,
        public readonly string $healthPath,
        public readonly int $worstCaseHeaderLineBytes,
        public readonly int $worstCaseSignedMessageBytes,
        public readonly int $softCapBytes,
        public readonly ?DateTimeImmutable $testForceWebhookFailureUntil,
        public readonly ?string $testErrorSubjectToken,
    ) {
    }

    public static function defaultPath(string $binDirectory): string
    {
        return dirname($binDirectory, 2) . '/private/config.json';
    }

    public static function load(string $path): self
    {
        self::assertPrivatePath($path);
        $contents = @file_get_contents($path);
        if (!is_string($contents)) {
            throw new InvalidArgumentException('Configuration unavailable');
        }
        try {
            $config = json_decode($contents, true, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException $error) {
            throw new InvalidArgumentException('Invalid configuration', 0, $error);
        }
        if (!is_array($config)) {
            throw new InvalidArgumentException('Invalid configuration');
        }

        return self::fromArray($config);
    }

    /** @param array<string,mixed> $config */
    public static function fromArray(array $config): self
    {
        $url = $config['webhook_url'] ?? null;
        $recipients = $config['error_recipients'] ?? null;
        $logPath = $config['log_path'] ?? null;
        $dedupPath = $config['dedup_path'] ?? null;
        $softCap = $config['soft_cap_bytes'] ?? 32_768;
        $pinnedTargets = $config['notification_pinned_targets'] ?? null;
        $notificationTargets = $config['notification_targets'] ?? null;
        $hmacEncoded = $config['system_mail_hmac_key'] ?? null;
        $testUntilValue = $config['test_force_webhook_failure_until'] ?? null;
        $testToken = $config['test_error_subject_token'] ?? null;
        if (!is_string($url) || filter_var($url, FILTER_VALIDATE_URL) === false
            || parse_url($url, PHP_URL_SCHEME) !== 'https'
            || parse_url($url, PHP_URL_HOST) !== 'webhook.worksmobile.com'
            || !is_array($recipients) || $recipients === []
            || !is_string($logPath) || !str_starts_with($logPath, '/')
            || ($dedupPath !== null && !is_string($dedupPath))
            || !is_int($softCap) || $softCap < 32) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        try {
            $canonicalRecipients = CanonicalEmail::many($recipients, false);
            $canonicalPinned = CanonicalEmail::many($pinnedTargets, true);
            $canonicalTargets = CanonicalEmail::many($notificationTargets, true);
        } catch (InvalidArgumentException) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        if ($recipients !== $canonicalRecipients || $pinnedTargets !== $canonicalPinned
            || $notificationTargets !== $canonicalTargets
            || !is_string($hmacEncoded)
            || preg_match('/\A[A-Za-z0-9_-]{43}\z/D', $hmacEncoded) !== 1) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        $toBytes = strlen(implode(',', $canonicalRecipients));
        $logBytes = strlen($logPath);
        $headerLineBytes = max(self::SYSTEM_MAIL_FIXED_HEADER_LINE_BYTES,
            strlen('To: ') + $toBytes);
        $signedMessageBytes = self::SYSTEM_MAIL_FIXED_MESSAGE_BYTES + $toBytes + $logBytes;
        if (count($canonicalRecipients) > self::MAX_RECIPIENTS || $toBytes > self::MAX_TO_BYTES
            || $logBytes > self::MAX_LOG_PATH_BYTES
            || $headerLineBytes > self::MAX_HEADER_LINE_BYTES
            || $signedMessageBytes > self::MAX_SIGNED_MESSAGE_BYTES) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        $hmacKey = base64_decode(strtr($hmacEncoded, '-_', '+/') . '=', true);
        if (!is_string($hmacKey) || strlen($hmacKey) !== 32
            || rtrim(strtr(base64_encode($hmacKey), '+/', '-_'), '=') !== $hmacEncoded) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        self::assertPrivatePath($logPath);
        $dedupPath ??= dirname($logPath) . '/delivery-dedup.json';
        self::assertPrivatePath($dedupPath);
        if (is_link($dedupPath)) {
            throw new InvalidArgumentException('Invalid configuration');
        }
        $testUntil = null;
        if ($testUntilValue !== null || $testToken !== null) {
            if (!is_string($testUntilValue) || !is_string($testToken)
                || preg_match('/\A[A-Za-z0-9_-]{32,128}\z/', $testToken) !== 1) {
                throw new InvalidArgumentException('Invalid configuration');
            }
            $testUntil = DateTimeImmutable::createFromFormat(DATE_ATOM, $testUntilValue);
            if (!$testUntil instanceof DateTimeImmutable || $testUntil->format(DATE_ATOM) !== $testUntilValue) {
                throw new InvalidArgumentException('Invalid configuration');
            }
        }

        return new self($url, $canonicalRecipients, $logPath, $dedupPath, $canonicalPinned,
            $canonicalTargets, $hmacKey, dirname($logPath) . '/delivery-health.json',
            $headerLineBytes, $signedMessageBytes, $softCap, $testUntil, $testToken);
    }

    public static function assertPrivatePath(string $path): void
    {
        if ($path === '' || !str_starts_with($path, '/')) {
            throw new InvalidArgumentException('Configuration path must be absolute');
        }
        self::rejectPublicHtml($path);
        $resolved = self::resolveExistingAncestor($path);
        if (is_string($resolved)) {
            self::rejectPublicHtml($resolved);
        }
    }

    private static function resolveExistingAncestor(string $path): ?string
    {
        $ancestor = $path;
        $suffix = [];
        while (!file_exists($ancestor) && !is_link($ancestor)) {
            $parent = dirname($ancestor);
            if ($parent === $ancestor) {
                return null;
            }
            array_unshift($suffix, basename($ancestor));
            $ancestor = $parent;
        }
        $resolved = realpath($ancestor);
        if (!is_string($resolved)) {
            return null;
        }

        return $suffix === [] ? $resolved : $resolved . '/' . implode('/', $suffix);
    }

    private static function rejectPublicHtml(string $path): void
    {
        $components = preg_split('~/+~', str_replace('\\', '/', strtolower($path)), -1, PREG_SPLIT_NO_EMPTY) ?: [];
        if (in_array('public_html', $components, true)) {
            throw new InvalidArgumentException('Configuration must be outside public_html');
        }
    }
}
