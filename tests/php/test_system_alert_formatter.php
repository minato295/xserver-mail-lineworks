<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';

use XserverMail\SystemAlertFormatter;

function formatterCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function formatterHasSingleTerminalLf(string $body): bool
{
    return str_ends_with($body, "\n") && !str_ends_with($body, "\n\n");
}

$formatter = new SystemAlertFormatter();
$failureAt = new DateTimeImmutable('2026-07-14T08:33:55Z');
$recoveryAt = new DateTimeImmutable('2026-07-14T08:35:10Z');

$expectedRealErrorBody = "LINE WORKSへのメール通知で障害が発生しました。\n"
    . "復旧するまで、LINE WORKSへ通知されない可能性があります。\n\n"
    . "【必要な対応】\n"
    . "Xserverのメールボックスで新着メールを直接確認してください。\n"
    . "このメールへの返信は不要です。\n\n"
    . "障害発生日時：2026年07月14日（火）17時33分55秒\n"
    . "障害内容：LINE WORKSに接続できませんでした。\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：transport_error\n"
    . "確認方法：Macの「Xserverメール通知管理」アプリで「同期診断」を実行してください。\n";
$expectedRealRecoveryBody = "LINE WORKSへのメール通知は復旧しました。\n"
    . "今後受信する対象メールは通常どおり通知されます。\n\n"
    . "障害中にLINE WORKSへ通知されなかったメールは自動では再通知されません。\n\n"
    . "【必要な対応】\n"
    . "障害発生日時から復旧日時までの新着メールを、\n"
    . "Xserverのメールボックスで確認してください。\n"
    . "このメールへの返信は不要です。\n\n"
    . "復旧日時：2026年07月14日（火）17時35分10秒\n"
    . "障害発生日時：2026年07月14日（火）17時33分55秒\n"
    . "障害内容：LINE WORKSに接続できませんでした。\n"
    . "現在の状態：正常\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：transport_error\n";
$expectedTestErrorBody = "これは管理者による障害通知メールの動作確認です。\n"
    . "実際の障害ではありません。対応は不要です。\n\n"
    . "テスト実行日時：2026年07月14日（火）17時33分55秒\n"
    . "確認結果：障害通知メールを正常に送信しました。\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：forced_test_failure\n";
$expectedTestRecoveryBody = "これは管理者による復旧通知メールの動作確認です。\n"
    . "実際の障害ではありません。対応は不要です。\n\n"
    . "テスト実行日時：2026年07月14日（火）17時35分10秒\n"
    . "確認結果：復旧通知メールを正常に送信しました。\n\n"
    . "【管理者向け情報】\n"
    . "原因コード：forced_test_failure\n";

$alert = $formatter->format('error', $failureAt, $failureAt, 'transport_error');
formatterCheck($alert['date'] === 'Tue, 14 Jul 2026 17:33:55 +0900', 'v2 Date must be exact JST');
formatterCheck($alert['test'] === false, 'A real transport failure must not be labelled as a test');
formatterCheck($alert['body'] === $expectedRealErrorBody, 'Real error body must match the approved copy exactly');

$bodyCases = [
    ['recovery', $recoveryAt, 'transport_error', $expectedRealRecoveryBody, false],
    ['error', $failureAt, 'forced_test_failure', $expectedTestErrorBody, true],
    ['recovery', $recoveryAt, 'forced_test_failure', $expectedTestRecoveryBody, true],
];
foreach ($bodyCases as [$type, $eventAt, $classification, $expectedBody, $test]) {
    $formatted = $formatter->format($type, $eventAt, $failureAt, $classification);
    formatterCheck($formatted['body'] === $expectedBody, $type . ' body must match the approved copy exactly');
    formatterCheck($formatted['test'] === $test, $type . ' test marker must match the classification');
}
foreach ([$expectedRealErrorBody, $expectedRealRecoveryBody, $expectedTestErrorBody, $expectedTestRecoveryBody] as $body) {
    formatterCheck(formatterHasSingleTerminalLf($body), 'Every body must have exactly one terminal LF');
    formatterCheck(!str_contains($body, "\r") && !str_contains($body, "\0"), 'Every body must contain LF-only safe text');
}

$jstCases = [
    ['2026-07-13T15:00:00Z', '2026年07月14日（火）00時00分00秒'],
    ['2026-07-31T15:00:00Z', '2026年08月01日（土）00時00分00秒'],
    ['2026-12-31T15:00:00Z', '2027年01月01日（金）00時00分00秒'],
    ['2028-02-29T14:59:59Z', '2028年02月29日（火）23時59分59秒'],
    ['2028-02-29T15:00:00Z', '2028年03月01日（水）00時00分00秒'],
];
foreach ($jstCases as [$utc, $expectedJst]) {
    $input = new DateTimeImmutable($utc);
    $originalValue = $input->format('Y-m-d H:i:s.uP');
    $originalTimezone = $input->getTimezone()->getName();
    $formatted = $formatter->format('error', $input, $input, 'transport_error');
    formatterCheck(str_contains($formatted['body'], '障害発生日時：' . $expectedJst), 'JST calendar boundary must be exact for ' . $utc);
    formatterCheck($input->format('Y-m-d H:i:s.uP') === $originalValue, 'Input value must remain immutable');
    formatterCheck($input->getTimezone()->getName() === $originalTimezone, 'Input timezone must remain immutable');
}

$weekdayCases = [
    ['2026-07-11T15:00:00Z', '2026年07月12日（日）00時00分00秒'],
    ['2026-07-12T15:00:00Z', '2026年07月13日（月）00時00分00秒'],
    ['2026-07-13T15:00:00Z', '2026年07月14日（火）00時00分00秒'],
    ['2026-07-14T15:00:00Z', '2026年07月15日（水）00時00分00秒'],
    ['2026-07-15T15:00:00Z', '2026年07月16日（木）00時00分00秒'],
    ['2026-07-16T15:00:00Z', '2026年07月17日（金）00時00分00秒'],
    ['2026-07-17T15:00:00Z', '2026年07月18日（土）00時00分00秒'],
];
foreach ($weekdayCases as [$utc, $expectedJst]) {
    $input = new DateTimeImmutable($utc);
    $formatted = $formatter->format('error', $input, $input, 'transport_error');
    formatterCheck(str_contains($formatted['body'], $expectedJst), 'Japanese weekday must be exact for ' . $utc);
}

$classificationCases = [
    'transport_error' => 'LINE WORKSに接続できませんでした。',
    'http_error' => 'LINE WORKSからエラーが返されました。',
    'rate_limited' => 'LINE WORKSの送信回数制限に達しました。',
    'invalid_webhook_url' => 'Webhook URLの設定が正しくありません。',
    'invalid_parameter' => '通知内容の設定に問題があります。',
    'missing_parameter' => '通知に必要な情報が不足しています。',
    'invalid_payload' => '受信メールを通知用に処理できませんでした。',
    'internal_error' => 'メール通知処理で内部エラーが発生しました。',
    'health_state_failure' => '障害状態を記録できませんでした。',
    'forced_test_failure' => '障害通知メールの動作確認です。',
    'unknown' => '原因不明のメール通知エラーです。',
];
foreach ($classificationCases as $classification => $display) {
    $formatted = $formatter->format('error', $failureAt, $failureAt, $classification);
    formatterCheck(str_contains($formatted['body'], $display), 'Classification display must be exact for ' . $classification);
    formatterCheck(str_contains($formatted['body'], '原因コード：' . $classification), 'Cause code must be exact for ' . $classification);
}

$unknown = $formatter->format('error', $failureAt, $failureAt, 'unknown');
foreach (['success', 'system_mail_suppressed', '', 'not_allowlisted'] as $unsafeClassification) {
    $formatted = $formatter->format('error', $failureAt, $failureAt, $unsafeClassification);
    formatterCheck($formatted === $unknown, 'Unsafe classification must degrade exactly to unknown');
    if ($unsafeClassification !== '') {
        formatterCheck(!str_contains($formatted['body'], $unsafeClassification), 'Unsafe input must not remain in the body');
    }
}

$invalidTypeRejected = false;
try {
    $formatter->format('failure', $failureAt, $failureAt, 'transport_error');
} catch (InvalidArgumentException) {
    $invalidTypeRejected = true;
}
formatterCheck($invalidTypeRejected, 'Only error and recovery types may be formatted');

echo "PASS: Japanese system alert formatter contract\n";
