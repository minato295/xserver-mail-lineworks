<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/vendor/autoload.php';

use XserverMail\SendmailClient;
use XserverMail\SendmailProcessAdapter;
use XserverMail\SendmailProcessHandle;
use XserverMail\NativeSendmailProcessAdapter;

// The handle and adapter contracts intentionally share the one task-scoped interface file.
interface_exists(SendmailProcessAdapter::class);

function sendmailCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

final class FakeSendmailHandle implements SendmailProcessHandle
{
    public string $stdin = '';
    public bool $stdinClosed = false;
    public bool $closed = false;
    public int $stdoutRead = 0;
    public int $stderrRead = 0;
    /** @var list<int> */
    public array $signals = [];
    public int $statusCalls = 0;
    private bool $running = true;
    private int $writeCalls = 0;
    private bool $stopPending = false;
    private int $stopPendingStatusChecks = 0;
    private int $zeroWritesRemaining;
    /** @var list<array<mixed>> */
    private array $statusResponses;
    private int $stdoutThrowsRemaining;
    private int $stderrThrowsRemaining;

    public function __construct(
        private string $stdout,
        private string $stderr,
        private readonly int $exitCode,
        private readonly bool $hang,
        private readonly ?int $invalidWriteAt = null,
        private readonly ?int $exitAfterWriteAt = null,
        private readonly int $stopAfterStatusChecks = 0,
        private readonly int $stdoutChunkBytes = 4096,
        private readonly int $stderrChunkBytes = 3072,
        int $zeroWrites = 0,
        array $statusResponses = [],
        private readonly bool $allowCloseWhileRunning = false,
        private readonly bool $ignoreSignals = false,
        private readonly ?int $throwStatusAfter = null,
        private readonly ?array $repeatStatus = null,
        int $stdoutThrows = 0,
        int $stderrThrows = 0,
    ) {
        $this->zeroWritesRemaining = $zeroWrites;
        $this->statusResponses = $statusResponses;
        $this->stdoutThrowsRemaining = $stdoutThrows;
        $this->stderrThrowsRemaining = $stderrThrows;
    }

    public function writeStdin(string $bytes): int
    {
        sendmailCheck(!$this->stdinClosed && !$this->closed, 'Client wrote after stdin/handle closure');
        ++$this->writeCalls;
        if ($this->zeroWritesRemaining > 0) {
            --$this->zeroWritesRemaining;
            return 0;
        }
        if ($this->writeCalls === $this->invalidWriteAt) {
            return -1;
        }
        $length = min(7, strlen($bytes));
        $this->stdin .= substr($bytes, 0, $length);
        if ($this->writeCalls === $this->exitAfterWriteAt) {
            $this->running = false;
        }
        return $length;
    }

    public function closeStdin(): void
    {
        $this->stdinClosed = true;
        if (!$this->hang) {
            $this->running = false;
        }
    }

    public function readStdout(): string
    {
        if ($this->stdoutThrowsRemaining > 0) {
            --$this->stdoutThrowsRemaining;
            throw new RuntimeException('STDOUT_PRIVATE_MARKER');
        }
        if ($this->running && $this->stopPending) {
            return '';
        }
        $chunk = substr($this->stdout, 0, $this->stdoutChunkBytes);
        $this->stdout = substr($this->stdout, strlen($chunk));
        $this->stdoutRead += strlen($chunk);
        return $chunk;
    }

    public function readStderr(): string
    {
        if ($this->stderrThrowsRemaining > 0) {
            --$this->stderrThrowsRemaining;
            throw new RuntimeException('STDERR_PRIVATE_MARKER');
        }
        if ($this->running && $this->stopPending) {
            return '';
        }
        $chunk = substr($this->stderr, 0, $this->stderrChunkBytes);
        $this->stderr = substr($this->stderr, strlen($chunk));
        $this->stderrRead += strlen($chunk);
        return $chunk;
    }

    public function status(): array
    {
        ++$this->statusCalls;
        if ($this->throwStatusAfter !== null && $this->statusCalls > $this->throwStatusAfter) {
            throw new RuntimeException('STDOUT_PRIVATE_MARKER');
        }
        if ($this->repeatStatus !== null) {
            return $this->repeatStatus;
        }
        if ($this->statusResponses !== []) {
            return array_shift($this->statusResponses);
        }
        if ($this->stopPending) {
            ++$this->stopPendingStatusChecks;
            if ($this->stopPendingStatusChecks >= $this->stopAfterStatusChecks) {
                $this->running = false;
                $this->stopPending = false;
            }
        }
        return ['running' => $this->running, 'exitCode' => $this->running ? null : $this->exitCode];
    }

    public function terminate(int $signal): void
    {
        $this->signals[] = $signal;
        if ($this->ignoreSignals) {
            return;
        }
        if (count($this->signals) >= 2) {
            if ($this->stopAfterStatusChecks > 0) {
                $this->stopPending = true;
            } else {
                $this->running = false;
            }
        }
    }

    public function close(): int
    {
        sendmailCheck(!$this->running || $this->allowCloseWhileRunning,
            'Client must reap only after the process stops');
        $this->stdinClosed = true;
        $this->closed = true;
        return $this->exitCode;
    }
}

final class FakeSendmailAdapter implements SendmailProcessAdapter
{
    /** @var list<list<string>> */
    public array $argv = [];
    /** @var list<FakeSendmailHandle> */
    public array $handles = [];

    public function __construct(
        private readonly int $exitCode = 0,
        private readonly bool $hang = false,
        private readonly ?int $invalidWriteAt = null,
        private readonly ?int $exitAfterWriteAt = null,
        private readonly int $stopAfterStatusChecks = 0,
        private readonly int $stdoutChunkBytes = 4096,
        private readonly int $stderrChunkBytes = 3072,
        private readonly int $zeroWrites = 0,
        private readonly array $statusResponses = [],
        private readonly bool $allowCloseWhileRunning = false,
        private readonly bool $ignoreSignals = false,
        private readonly ?int $throwStatusAfter = null,
        private readonly ?array $repeatStatus = null,
        private readonly int $stdoutThrows = 0,
        private readonly int $stderrThrows = 0,
    ) {
    }

    public function start(array $argv): SendmailProcessHandle
    {
        $this->argv[] = $argv;
        $handle = new FakeSendmailHandle(
            str_repeat('O', 65_515) . 'STDOUT_PRIVATE_MARKER',
            str_repeat('E', 65_515) . 'STDERR_PRIVATE_MARKER',
            $this->exitCode,
            $this->hang,
            $this->invalidWriteAt,
            $this->exitAfterWriteAt,
            $this->stopAfterStatusChecks,
            $this->stdoutChunkBytes,
            $this->stderrChunkBytes,
            $this->zeroWrites,
            $this->statusResponses,
            $this->allowCloseWhileRunning,
            $this->ignoreSignals,
            $this->throwStatusAfter,
            $this->repeatStatus,
            $this->stdoutThrows,
            $this->stderrThrows,
        );
        $this->handles[] = $handle;
        return $handle;
    }
}

final class ThrowingSendmailAdapter implements SendmailProcessAdapter
{
    public function start(array $argv): SendmailProcessHandle
    {
        throw new InvalidArgumentException('STDOUT_PRIVATE_MARKER');
    }
}

final class ChainedFixedTextSendmailAdapter implements SendmailProcessAdapter
{
    public function start(array $argv): SendmailProcessHandle
    {
        throw new RuntimeException('Sendmail failed', 0, new RuntimeException('STDOUT_PRIVATE_MARKER'));
    }
}

function expectSendmailFailure(callable $operation, string $expected): RuntimeException
{
    try {
        $operation();
    } catch (RuntimeException $error) {
        sendmailCheck($error->getMessage() === $expected, 'Sendmail failure message must be fixed');
        sendmailCheck(!str_contains($error->getMessage(), 'PRIVATE_MARKER'), 'Process output must not leak');
        sendmailCheck(!str_contains($error->getMessage(), 'MESSAGE_PRIVATE_MARKER'), 'Message bytes must not leak');
        return $error;
    }
    throw new RuntimeException('Expected sendmail failure was not thrown');
}

$message = "To: operator@example.invalid\r\n\r\nMESSAGE_PRIVATE_MARKER\r\n";
$defaultWaiterAdapter = new FakeSendmailAdapter();
$defaultWaiterClient = new SendmailClient(
    $defaultWaiterAdapter,
    static fn (): float => 0.0,
);
$defaultWaiterClient->send($message);
sendmailCheck(
    $defaultWaiterAdapter->handles[0]->statusCalls > 2,
    'Default waiter must allow a running process to reach its successful exit status',
);

$adapter = new FakeSendmailAdapter();
$client = new SendmailClient($adapter, static fn (): float => 0.0, static function (): void {});
$client->send($message);
$handle = $adapter->handles[0];
sendmailCheck($adapter->argv === [["/usr/sbin/sendmail", '-t', '-i']], 'Only exact sendmail argv may be used');
sendmailCheck($handle->stdin === $message, 'Partial stdin writes must deliver every byte in order');
sendmailCheck($handle->stdinClosed, 'Sendmail stdin must close after the complete message');
sendmailCheck($handle->stdoutRead === 65_536 && $handle->stderrRead === 65_536,
    'All 64 KiB stdout and stderr bytes must be drained');
sendmailCheck($handle->closed, 'Successful process must be reaped and all pipes closed');

$transientZeroAdapter = new FakeSendmailAdapter(0, false, null, null, 0, 4096, 3072, 2);
$transientZeroClient = new SendmailClient($transientZeroAdapter, static fn (): float => 0.0,
    static function (): void {});
$transientZeroClient->send($message);
$transientZeroHandle = $transientZeroAdapter->handles[0];
sendmailCheck($transientZeroHandle->stdin === $message && $transientZeroHandle->stdinClosed
    && $transientZeroHandle->closed, 'Transient zero-byte writes must later complete the exact message successfully');

$persistentClock = 0.0;
$persistentZeroAdapter = new FakeSendmailAdapter(143, true, null, null, 0, 4096, 3072, PHP_INT_MAX);
$persistentZeroClient = new SendmailClient(
    $persistentZeroAdapter,
    static function () use (&$persistentClock): float { return $persistentClock++; },
    static function (): void {},
);
expectSendmailFailure(static fn () => $persistentZeroClient->send($message), 'Sendmail timed out');
$persistentZeroHandle = $persistentZeroAdapter->handles[0];
sendmailCheck($persistentClock === 16.0 && $persistentZeroHandle->stdin === ''
    && $persistentZeroHandle->signals === [15, 9], 'Persistent zero-byte writes must reach exact 15s timeout and TERM/KILL');
sendmailCheck($persistentZeroHandle->stdoutRead === 65_536 && $persistentZeroHandle->stderrRead === 65_536
    && $persistentZeroHandle->stdinClosed && $persistentZeroHandle->closed,
    'Persistent zero-byte timeout must fully drain, close every pipe, and reap');

$ignoredSignalClock = 0.0;
$ignoredSignalAdapter = new FakeSendmailAdapter(
    exitCode: 143,
    hang: true,
    zeroWrites: PHP_INT_MAX,
    allowCloseWhileRunning: true,
    ignoreSignals: true,
    throwStatusAfter: 40,
);
$ignoredSignalClient = new SendmailClient(
    $ignoredSignalAdapter,
    static function () use (&$ignoredSignalClock): float { return $ignoredSignalClock++; },
    static function (): void {},
);
expectSendmailFailure(static fn () => $ignoredSignalClient->send($message), 'Sendmail timed out');
$ignoredSignalHandle = $ignoredSignalAdapter->handles[0];
sendmailCheck($ignoredSignalHandle->statusCalls <= 20,
    'Main timeout must leave the running loop after bounded TERM/KILL observations');
sendmailCheck($ignoredSignalHandle->stdoutRead === 65_536 && $ignoredSignalHandle->stderrRead === 65_536
    && $ignoredSignalHandle->stdinClosed && $ignoredSignalHandle->closed,
    'Ignored-signal timeout must best-effort drain, close every pipe, and attempt final reap');

$frozenClockCalls = 0;
$frozenClockAdapter = new FakeSendmailAdapter(
    exitCode: 143, hang: true, invalidWriteAt: 1, allowCloseWhileRunning: true, ignoreSignals: true,
);
$frozenClockClient = new SendmailClient(
    $frozenClockAdapter,
    static function () use (&$frozenClockCalls): float {
        if (++$frozenClockCalls > 80) {
            throw new RuntimeException('CLOCK_PRIVATE_MARKER');
        }
        return 0.0;
    },
    static function (): void { throw new RuntimeException('WAITER_PRIVATE_MARKER'); },
);
expectSendmailFailure(static fn () => $frozenClockClient->send($message), 'Sendmail failed');
$frozenClockHandle = $frozenClockAdapter->handles[0];
sendmailCheck($frozenClockCalls <= 70 && $frozenClockHandle->statusCalls <= 64
    && $frozenClockHandle->stdoutRead === 65_536 && $frozenClockHandle->stderrRead === 65_536
    && $frozenClockHandle->closed,
    'Frozen cleanup clocks and throwing waiters must have independent bounded polling and close');

foreach (['regressing' => false, 'nan' => true] as $clockMode => $returnsNan) {
    $badClockCalls = 0;
    $badClockAdapter = new FakeSendmailAdapter(
        exitCode: 143, hang: true, invalidWriteAt: 1, allowCloseWhileRunning: true, ignoreSignals: true,
    );
    $badClockClient = new SendmailClient(
        $badClockAdapter,
        static function () use (&$badClockCalls, $returnsNan): float {
            ++$badClockCalls;
            return $returnsNan ? NAN : 100.0 - $badClockCalls;
        },
        static function (): void {},
    );
    expectSendmailFailure(static fn () => $badClockClient->send($message), 'Sendmail failed');
    $badClockHandle = $badClockAdapter->handles[0];
    sendmailCheck($badClockCalls <= 4 && $badClockHandle->statusCalls <= 1
        && $badClockHandle->stdoutRead === 65_536 && $badClockHandle->stderrRead === 65_536
        && $badClockHandle->closed,
        "{$clockMode} cleanup clocks must fail closed without unbounded polling or skipped drain");
}

foreach (['malformed' => [], 'throwing' => null] as $statusMode => $repeatStatus) {
    $uncertainClock = 0.0;
    $uncertainAdapter = new FakeSendmailAdapter(
        exitCode: 143,
        hang: true,
        invalidWriteAt: 1,
        allowCloseWhileRunning: true,
        ignoreSignals: true,
        throwStatusAfter: $statusMode === 'throwing' ? 0 : null,
        repeatStatus: $repeatStatus,
    );
    $uncertainClient = new SendmailClient(
        $uncertainAdapter,
        static function () use (&$uncertainClock): float { return $uncertainClock += 0.1; },
        static function (): void {},
    );
    expectSendmailFailure(static fn () => $uncertainClient->send($message), 'Sendmail failed');
    $uncertainHandle = $uncertainAdapter->handles[0];
    sendmailCheck($uncertainHandle->stdoutRead === 65_536 && $uncertainHandle->stderrRead === 65_536
        && $uncertainHandle->stdinClosed && $uncertainHandle->closed,
        "Persistent {$statusMode} process status must still best-effort drain, close, and reap");
}

$readRetryAdapter = new FakeSendmailAdapter(
    exitCode: 143, hang: true, invalidWriteAt: 1, stdoutThrows: 1, stderrThrows: 1,
);
$readRetryClient = new SendmailClient($readRetryAdapter, static fn (): float => 0.0,
    static function (): void {});
expectSendmailFailure(static fn () => $readRetryClient->send($message), 'Sendmail failed');
$readRetryHandle = $readRetryAdapter->handles[0];
sendmailCheck($readRetryHandle->stdoutRead === 65_536 && $readRetryHandle->stderrRead === 65_536
    && $readRetryHandle->closed,
    'Transient cleanup read exceptions must be retried instead of treated as EOF');

$nonzeroAdapter = new FakeSendmailAdapter(75);
$nonzeroClient = new SendmailClient($nonzeroAdapter, static fn (): float => 0.0, static function (): void {});
expectSendmailFailure(static fn () => $nonzeroClient->send($message), 'Sendmail failed');
sendmailCheck($nonzeroAdapter->handles[0]->closed, 'Nonzero process must be reaped and all pipes closed');
sendmailCheck($nonzeroAdapter->handles[0]->stdoutRead === 65_536
    && $nonzeroAdapter->handles[0]->stderrRead === 65_536, 'Nonzero process output must still be fully drained');

$earlyExitAdapter = new FakeSendmailAdapter(0, true, null, 2);
$earlyExitClient = new SendmailClient($earlyExitAdapter, static fn (): float => 0.0, static function (): void {});
expectSendmailFailure(static fn () => $earlyExitClient->send($message), 'Sendmail failed');
$earlyExitHandle = $earlyExitAdapter->handles[0];
sendmailCheck(strlen($earlyExitHandle->stdin) < strlen($message),
    'Early-exit fixture must stop before the complete message is written');
sendmailCheck($earlyExitHandle->stdoutRead === 65_536 && $earlyExitHandle->stderrRead === 65_536
    && $earlyExitHandle->closed, 'Early zero exit must still drain and reap before fixed failure');

$midLoopAdapter = new FakeSendmailAdapter(70, true, 2);
$midLoopClient = new SendmailClient($midLoopAdapter, static fn (): float => 0.0, static function (): void {});
expectSendmailFailure(static fn () => $midLoopClient->send($message), 'Sendmail failed');
$midLoopHandle = $midLoopAdapter->handles[0];
sendmailCheck($midLoopHandle->stdoutRead === 65_536 && $midLoopHandle->stderrRead === 65_536,
    'Mid-loop failure cleanup must fully drain both 64 KiB output streams');
sendmailCheck($midLoopHandle->signals === [15, 9],
    'Mid-loop failure cleanup must stop the process with bounded TERM/KILL');
sendmailCheck($midLoopHandle->stdinClosed && $midLoopHandle->closed,
    'Mid-loop failure cleanup must close stdin and finally reap every pipe');

$longDrainAdapter = new FakeSendmailAdapter(70, true, 2, null, 0, 1, 1);
$longDrainClient = new SendmailClient($longDrainAdapter, static fn (): float => 0.0, static function (): void {});
expectSendmailFailure(static fn () => $longDrainClient->send($message), 'Sendmail failed');
$longDrainHandle = $longDrainAdapter->handles[0];
sendmailCheck($longDrainHandle->stdoutRead === 65_536 && $longDrainHandle->stderrRead === 65_536,
    'Failure cleanup must drain more than 256 finite output chunks without truncation');
sendmailCheck($longDrainHandle->closed, 'Long finite output cleanup must finally reap and close every pipe');

$delayedOutputAdapter = new FakeSendmailAdapter(70, true, 2, null, 1);
$delayedOutputClient = new SendmailClient($delayedOutputAdapter, static fn (): float => 0.0,
    static function (): void {});
expectSendmailFailure(static fn () => $delayedOutputClient->send($message), 'Sendmail failed');
$delayedOutputHandle = $delayedOutputAdapter->handles[0];
sendmailCheck($delayedOutputHandle->stdoutRead === 65_536 && $delayedOutputHandle->stderrRead === 65_536,
    'Empty nonblocking reads while running must not hide delayed output after process stop');
sendmailCheck($delayedOutputHandle->statusCalls >= 2 && $delayedOutputHandle->closed,
    'Cleanup must confirm stopped status before draining to EOF and final reap');

$thirdStatusAdapter = new FakeSendmailAdapter(70, true, 2, null, 3);
$thirdStatusClock = 0.0;
$thirdStatusWaits = 0;
$thirdStatusClient = new SendmailClient(
    $thirdStatusAdapter,
    static function () use (&$thirdStatusClock): float { return $thirdStatusClock += 0.1; },
    static function () use (&$thirdStatusWaits): void { ++$thirdStatusWaits; },
);
expectSendmailFailure(static fn () => $thirdStatusClient->send($message), 'Sendmail failed');
$thirdStatusHandle = $thirdStatusAdapter->handles[0];
sendmailCheck($thirdStatusHandle->statusCalls >= 3 && $thirdStatusHandle->stdoutRead === 65_536
    && $thirdStatusHandle->stderrRead === 65_536,
    'Cleanup must wait beyond two immediate status checks and then fully drain stopped output');
sendmailCheck($thirdStatusHandle->stdinClosed && $thirdStatusHandle->closed,
    'Delayed third-status stop must close every pipe and finally reap');
sendmailCheck($thirdStatusHandle->signals === [15, 9, 9, 9] && $thirdStatusWaits === 3,
    'Delayed stop confirmation must use bounded waiter polls and repeated KILL');

$throwingClient = new SendmailClient(new ThrowingSendmailAdapter(), static fn (): float => 0.0,
    static function (): void {});
expectSendmailFailure(static fn () => $throwingClient->send($message), 'Sendmail failed');
$chainedClient = new SendmailClient(new ChainedFixedTextSendmailAdapter(), static fn (): float => 0.0,
    static function (): void {});
$normalizedError = expectSendmailFailure(static fn () => $chainedClient->send($message), 'Sendmail failed');
sendmailCheck($normalizedError->getPrevious() === null,
    'External fixed-text exception must be replaced with a fresh unchained fixed exception');

$invalidStatuses = [
    'missing-all' => [],
    'missing-exit' => ['running' => false],
    'extra-key' => ['running' => false, 'exitCode' => 0, 'extra' => true],
    'running-type' => ['running' => 0, 'exitCode' => null],
    'exit-type' => ['running' => false, 'exitCode' => '0'],
    'running-with-exit' => ['running' => true, 'exitCode' => 0],
    'negative-exit' => ['running' => false, 'exitCode' => -1],
];
foreach ($invalidStatuses as $statusName => $invalidStatus) {
    $statusAdapter = new FakeSendmailAdapter(0, true, null, null, 0, 4096, 3072, 0,
        [$invalidStatus], true);
    $statusClock = 0.0;
    $statusClient = new SendmailClient(
        $statusAdapter,
        static function () use (&$statusClock): float { return $statusClock += 0.1; },
        static function (): void {},
    );
    $warnings = [];
    set_error_handler(static function (int $severity, string $message) use (&$warnings): bool {
        $warnings[] = $message;
        return true;
    });
    try {
        $statusError = expectSendmailFailure(static fn () => $statusClient->send($message), 'Sendmail failed');
    } finally {
        restore_error_handler();
    }
    sendmailCheck($warnings === [], $statusName . ' malformed status must not emit warnings');
    sendmailCheck($statusError->getPrevious() === null, $statusName . ' status failure must be fresh and unchained');
    $statusHandle = $statusAdapter->handles[0];
    sendmailCheck($statusHandle->stdoutRead === 65_536 && $statusHandle->stderrRead === 65_536
        && $statusHandle->stdinClosed && $statusHandle->closed,
        $statusName . ' status failure must cleanly drain, close, and reap');
}

$clock = 0.0;
$hangAdapter = new FakeSendmailAdapter(143, true);
$hangClient = new SendmailClient(
    $hangAdapter,
    static function () use (&$clock): float { return $clock++; },
    static function (): void {},
);
expectSendmailFailure(static fn () => $hangClient->send($message), 'Sendmail timed out');
$hangHandle = $hangAdapter->handles[0];
sendmailCheck($hangHandle->signals === [15, 9], 'Timeout must try TERM then KILL when the first terminate is ignored');
sendmailCheck($clock === 16.0, 'Default timeout must trigger at injected monotonic 15 seconds');
sendmailCheck($hangHandle->closed && $hangHandle->stdinClosed, 'Timed-out process must be finally reaped with all pipes closed');
sendmailCheck($hangHandle->stdoutRead === 65_536 && $hangHandle->stderrRead === 65_536,
    'Timed-out process output must be fully drained without leakage');

$boundaryAdapter = new FakeSendmailAdapter();
$boundaryClient = new SendmailClient($boundaryAdapter, static fn (): float => 0.0, static function (): void {});
$boundaryClient->send(str_repeat('m', 65_536));
sendmailCheck(strlen($boundaryAdapter->handles[0]->stdin) === 65_536,
    'Exactly 65536 message bytes must be accepted');
$started = count($boundaryAdapter->handles);
try {
    $boundaryClient->send(str_repeat('m', 65_537));
    throw new RuntimeException('Oversized sendmail message was accepted');
} catch (InvalidArgumentException $error) {
    sendmailCheck($error->getMessage() === 'Invalid sendmail message', 'Oversize error must be fixed');
}
sendmailCheck(count($boundaryAdapter->handles) === $started, 'Oversized message must fail before process start');

foreach ([0, -1] as $invalidTimeout) {
    try {
        $boundaryClient->send('message', $invalidTimeout);
        throw new RuntimeException('Invalid sendmail timeout was accepted');
    } catch (InvalidArgumentException $error) {
        sendmailCheck($error->getMessage() === 'Invalid sendmail timeout', 'Timeout input error must be fixed');
    }
}

$reflection = new ReflectionClass(SendmailClient::class);
foreach ($reflection->getConstants() as $name => $value) {
    if (str_contains($name, 'DIAGNOSTIC')) {
        sendmailCheck(is_int($value) && $value <= 8192, 'Diagnostic retention cap must not exceed 8192 per stream');
    }
}
$clientSource = file_get_contents(dirname(__DIR__, 2) . '/src/SendmailClient.php');
sendmailCheck(is_string($clientSource) && !str_contains($clientSource, 'class SendmailClientOutcome'),
    'Sendmail client file must not expose an extra namespace-level outcome symbol');

$nativeSource = file_get_contents(dirname(__DIR__, 2) . '/src/NativeSendmailProcessAdapter.php');
sendmailCheck(is_string($nativeSource)
    && str_contains($nativeSource, 'count($pipes) !== 3')
    && str_contains($nativeSource, 'array_keys($pipes) !== [0, 1, 2]')
    && str_contains($nativeSource, '$this->cleanupFailedStart($process, $pipes);'),
    'Partially successful native process start must route through explicit resource cleanup');
$cleanupPipes = [];
$cleanupProcess = proc_open([PHP_BINARY, '-r', 'usleep(5000000);'], [
    0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'],
], $cleanupPipes);
sendmailCheck(is_resource($cleanupProcess) && count($cleanupPipes) === 3,
    'Native cleanup fixture process must start');
$cleanupMethod = new ReflectionMethod(NativeSendmailProcessAdapter::class, 'cleanupFailedStart');
$cleanupMethod->invoke(new NativeSendmailProcessAdapter(), $cleanupProcess, $cleanupPipes);
sendmailCheck(!is_resource($cleanupProcess)
    && array_filter($cleanupPipes, 'is_resource') === [],
    'Native failed-start cleanup must close every pipe, force-stop, and reap the child');

echo "PASS: bounded sendmail client and process contract\n";
