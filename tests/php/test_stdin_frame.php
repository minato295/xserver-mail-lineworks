<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';

use XserverMail\StdinFrame;

function frameCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

/** @return resource */
function frameStream(string $bytes)
{
    $stream = fopen('php://temp', 'w+b');
    if (!is_resource($stream)) {
        throw new RuntimeException('Unable to create frame fixture');
    }
    frameCheck(fwrite($stream, $bytes) === strlen($bytes), 'Unable to write frame fixture');
    rewind($stream);
    return $stream;
}

function frameBytes(string $config, string $message = '', int $high = 0, ?int $low = null): string
{
    return StdinFrame::MAGIC . pack('NN', $high, $low ?? strlen($config)) . $config . $message;
}

function frameRejects(string $bytes, string $message): void
{
    $stream = frameStream($bytes);
    try {
        StdinFrame::decode($stream);
    } catch (InvalidArgumentException) {
        fclose($stream);
        return;
    }
    fclose($stream);
    throw new RuntimeException($message);
}

/**
 * Models the JSON shape check at the entrypoint boundary that consumes decode().
 *
 * @return array<string,mixed>
 */
function frameEntrypointConfig(string $configJson): array
{
    $stream = frameStream(frameBytes($configJson));
    try {
        $frame = StdinFrame::decode($stream);
    } finally {
        fclose($stream);
    }
    try {
        $value = json_decode($frame['configJson'], true, 32, JSON_THROW_ON_ERROR);
    } catch (JsonException $error) {
        throw new InvalidArgumentException('Invalid configuration', 0, $error);
    }
    if (!is_array($value) || array_is_list($value)) {
        throw new InvalidArgumentException('Invalid configuration');
    }
    return $value;
}

function frameEntrypointRejects(string $configJson, string $message): void
{
    try {
        frameEntrypointConfig($configJson);
    } catch (InvalidArgumentException) {
        return;
    }
    throw new RuntimeException($message);
}

$config = '{"webhook_url":"https://webhook.worksmobile.com/message/test-placeholder"}';
$message = "From: test@example.invalid\r\n\r\nA\0B\xff";
$stream = frameStream(frameBytes($config, $message));
$decoded = StdinFrame::decode($stream);
fclose($stream);
frameCheck($decoded === ['configJson' => $config, 'message' => $message], 'frame bytes changed');
frameCheck(StdinFrame::MAX_CONFIG_BYTES === 65536, 'config limit changed');
frameCheck(StdinFrame::MAX_MESSAGE_BYTES === 10 * 1024 * 1024, 'message limit changed');

$wrongMagic = StdinFrame::MAGIC;
$wrongMagic[0] = 'Y';
frameRejects($wrongMagic . pack('NN', 0, 2) . '{}', 'wrong magic accepted');
frameRejects(substr(StdinFrame::MAGIC, 0, -1), 'magic EOF accepted');
frameRejects(StdinFrame::MAGIC . "\0\0\0\0\0\0\0", 'header EOF accepted');
frameRejects(frameBytes('{}', '', 1), 'nonzero high length word accepted');
frameRejects(frameBytes('', '', 0, 0), 'zero config length accepted');
frameRejects(frameBytes('x', '', 0, 1), 'one-byte config length accepted');
frameRejects(frameBytes('', '', 0, StdinFrame::MAX_CONFIG_BYTES + 1), 'oversize config length accepted');
frameRejects(frameBytes('{}', '', 0, 3), 'config EOF accepted');
frameRejects(frameBytes('{}', str_repeat('x', StdinFrame::MAX_MESSAGE_BYTES + 1)), 'oversize message accepted');

frameEntrypointRejects('null', 'scalar JSON accepted at entrypoint boundary');
frameEntrypointRejects('[]', 'list JSON accepted at entrypoint boundary');
frameEntrypointRejects('{', 'invalid JSON accepted at entrypoint boundary');

echo "PASS: stdin frame decoder\n";
