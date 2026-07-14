<?php

declare(strict_types=1);

namespace XserverMail;

use DateTimeImmutable;
use DateTimeZone;
use InvalidArgumentException;

final class SystemAlertFormatter
{
    /** @var array<string,string> */
    private const CLASSIFICATION_DISPLAYS = [
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

    /** @var list<string> */
    private const JAPANESE_WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];

    /** @return array{date:string,body:string,test:bool} */
    public function format(
        string $type,
        DateTimeImmutable $eventAtUtc,
        DateTimeImmutable $failureAtUtc,
        string $classification,
    ): array {
        if ($type !== 'error' && $type !== 'recovery') {
            throw new InvalidArgumentException('Invalid system alert type');
        }

        if (!isset(self::CLASSIFICATION_DISPLAYS[$classification])) {
            $classification = 'unknown';
        }

        $eventAtJst = $eventAtUtc->setTimezone(new DateTimeZone('Asia/Tokyo'));
        $failureAtJst = $failureAtUtc->setTimezone(new DateTimeZone('Asia/Tokyo'));
        $test = $classification === 'forced_test_failure';

        return [
            'date' => $eventAtJst->format('D, d M Y H:i:s O'),
            'body' => $test
                ? $this->testBody($type, $eventAtJst)
                : $this->realBody($type, $eventAtJst, $failureAtJst, $classification),
            'test' => $test,
        ];
    }

    private function realBody(
        string $type,
        DateTimeImmutable $eventAtJst,
        DateTimeImmutable $failureAtJst,
        string $classification,
    ): string {
        $display = self::CLASSIFICATION_DISPLAYS[$classification];
        $failureAt = $this->japaneseDate($failureAtJst);
        if ($type === 'error') {
            return "LINE WORKSへのメール通知で障害が発生しました。\n"
                . "復旧するまで、LINE WORKSへ通知されない可能性があります。\n\n"
                . "【必要な対応】\n"
                . "Xserverのメールボックスで新着メールを直接確認してください。\n"
                . "このメールへの返信は不要です。\n\n"
                . "障害発生日時：{$failureAt}\n"
                . "障害内容：{$display}\n\n"
                . "【管理者向け情報】\n"
                . "原因コード：{$classification}\n"
                . "確認方法：Macの「Xserverメール通知管理」アプリで「同期診断」を実行してください。\n";
        }

        $eventAt = $this->japaneseDate($eventAtJst);
        return "LINE WORKSへのメール通知は復旧しました。\n"
            . "今後受信する対象メールは通常どおり通知されます。\n\n"
            . "障害中にLINE WORKSへ通知されなかったメールは自動では再通知されません。\n\n"
            . "【必要な対応】\n"
            . "障害発生日時から復旧日時までの新着メールを、\n"
            . "Xserverのメールボックスで確認してください。\n"
            . "このメールへの返信は不要です。\n\n"
            . "復旧日時：{$eventAt}\n"
            . "障害発生日時：{$failureAt}\n"
            . "障害内容：{$display}\n"
            . "現在の状態：正常\n\n"
            . "【管理者向け情報】\n"
            . "原因コード：{$classification}\n";
    }

    private function testBody(string $type, DateTimeImmutable $eventAtJst): string
    {
        $eventAt = $this->japaneseDate($eventAtJst);
        if ($type === 'error') {
            return "これは管理者による障害通知メールの動作確認です。\n"
                . "実際の障害ではありません。対応は不要です。\n\n"
                . "テスト実行日時：{$eventAt}\n"
                . "確認結果：障害通知メールを正常に送信しました。\n\n"
                . "【管理者向け情報】\n"
                . "原因コード：forced_test_failure\n";
        }

        return "これは管理者による復旧通知メールの動作確認です。\n"
            . "実際の障害ではありません。対応は不要です。\n\n"
            . "テスト実行日時：{$eventAt}\n"
            . "確認結果：復旧通知メールを正常に送信しました。\n\n"
            . "【管理者向け情報】\n"
            . "原因コード：forced_test_failure\n";
    }

    private function japaneseDate(DateTimeImmutable $date): string
    {
        $weekday = self::JAPANESE_WEEKDAYS[(int) $date->format('w')];
        return $date->format('Y年m月d日（') . $weekday . $date->format('）H時i分s秒');
    }
}
