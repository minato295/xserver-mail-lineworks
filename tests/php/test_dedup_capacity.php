<?php
declare(strict_types=1);
require dirname(__DIR__, 2) . '/vendor/autoload.php';

function capacityCheck(bool $value, string $reason): void {
    if (!$value) throw new RuntimeException($reason);
}
$directory = sys_get_temp_dir() . '/dedup-capacity-' . bin2hex(random_bytes(8));
mkdir($directory, 0700);
$directory = realpath($directory);
$path = $directory . '/delivery-dedup.json';
try {
    $claims = [];
    for ($i = 0; $i < 9280; ++$i) {
        $claims[hash('sha256', (string)$i)] = ['status'=>'committed', 'timestamp'=>1700000000];
    }
    file_put_contents($path, json_encode((object)$claims) . "\n");
    chmod($path, 0600);
    capacityCheck(filesize($path) > 1048576, 'Fixture must exceed old read limit');
    $store = new XserverMail\DeliveryDeduplicator($path);
    $now = new DateTimeImmutable('@1700001000');
    $hash = hash('sha256', 'new-mail');
    $token = $store->reserve($hash, $now);
    capacityCheck(is_string($token), 'Expired oversized legacy state must recover');
    $store->commit($hash, $token, $now);
    capacityCheck($store->reserve($hash, $now) === null, 'Recovered state must suppress duplicate');
    clearstatcache(true, $path);
    capacityCheck(filesize($path) < 1048576, 'Recovery must compact the state');

    // A full live state must remain readable; do not evict active reservations.
    array_pop($claims);
    $bytes = json_encode((object)$claims) . "\n";
    file_put_contents($path, $bytes);
    $caught = false;
    try { $store->reserve(hash('sha256', 'overflow'), new DateTimeImmutable('@1700000001')); }
    catch (RuntimeException) { $caught = true; }
    capacityCheck($caught, 'Capacity overflow must fail before publishing oversized state');
    capacityCheck(file_get_contents($path) === $bytes, 'Live claims must remain intact on overflow');
    capacityCheck(is_string($store->reserve(hash('sha256', 'after-expiry'), $now)),
        'Transient capacity exhaustion must self-recover after expiration');
    echo "PASS: bounded deduplication and legacy capacity recovery\n";
} finally {
    foreach (glob($directory . '/{*,.*}', GLOB_BRACE) ?: [] as $entry) {
        if (is_file($entry)) unlink($entry);
    }
    rmdir($directory);
}
