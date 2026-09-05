<?php
declare(strict_types=1);
require dirname(__DIR__, 2) . '/vendor/autoload.php';
require_once dirname(__DIR__, 2) . '/src/SendmailProcessAdapter.php';
use XserverMail\{PrivateStateFilesystem,SendmailProcessAdapter,SendmailProcessHandle,DeliveryHealthMonitor,OperationalLogger,SendmailClient,SystemMailAuthenticator,DeliveryOutbox,DeliveryRecovery,WebhookClient,NotifierConfig};

function auditCheck(bool $ok, string $why): void { if (!$ok) throw new RuntimeException($why); }
final class AuditMemoryFs implements PrivateStateFilesystem {
    public array $files = []; public bool $fail = false;
    public function withExclusiveLock(string $p, callable $f): mixed { return $f(); }
    public function assertExclusiveLockCurrent(): void {}
    public function readRegular(string $p, int $n): ?string { return $this->files[$p] ?? null; }
    public function replaceAtomic(string $p, string $b, int $m): void {
        if ($this->fail) throw new RuntimeException('synthetic storage failure');
        $this->files[$p] = $b;
    }
}
$dir = sys_get_temp_dir() . '/notifier-startup-' . bin2hex(random_bytes(8));
mkdir($dir,0700); $dir = realpath($dir); mkdir($dir.'/state',0700);
$config = ['webhook_url'=>'https://webhook.worksmobile.com/message/test',
    'error_recipients'=>['admin@example.invalid'],'notification_targets'=>['target@example.invalid'],
    'notification_pinned_targets'=>[],'system_mail_hmac_key'=>rtrim(base64_encode(str_repeat('a',32)),'='),
    'log_path'=>$dir.'/state/log.json'];
try {
    // Real CLI, framing, reporter and state filesystem. Disable every real sending transport.
    $prelude=$dir.'/prelude.php';
    file_put_contents($prelude,'<?php namespace XserverMail; function posix_getpwuid($uid){return ["dir"=>getenv("TEST_HOME")];}');
    $json=json_encode($config);
    $frame=XserverMail\StdinFrame::MAGIC.pack('NN',0,strlen($json)).$json.str_repeat('x',10485761);
    $pipes=[];
    $process=proc_open([PHP_BINARY,'-d','auto_prepend_file='.$prelude,'-d',
        'disable_functions=proc_open,popen,mail,curl_exec,curl_multi_exec',
        dirname(__DIR__,2).'/bin/mail-to-lineworks.php'],[['pipe','r'],['pipe','w'],['pipe','w']],$pipes,null,
        ['TEST_HOME'=>$dir,'MAIL_NOTIFIER_STDIN_FRAME'=>'1']);
    auditCheck(is_resource($process),'CLI must start');
    $offset=0; while($offset<strlen($frame)) { $n=fwrite($pipes[0],substr($frame,$offset,65536)); if(!$n)break; $offset+=$n; }
    fclose($pipes[0]); $stdout=stream_get_contents($pipes[1]);fclose($pipes[1]);
    $stderr=stream_get_contents($pipes[2]);fclose($pipes[2]);$code=proc_close($process);
    $log=is_file($config['log_path'])?file_get_contents($config['log_path']):'';
    auditCheck(str_contains($log,'input_too_large'),'Oversize framed input must record an explicit size failure');
    auditCheck(str_contains($log,'"stage":"input"'),'Oversize failure must identify input stage');
    auditCheck(!str_contains($log,str_repeat('x',100)),'Diagnostic must not contain input');
    auditCheck($code===0 && $stdout==='' && $stderr==='','Handled input failure must not interrupt mailbox copy delivery');
    $health=json_decode(file_get_contents($dir.'/state/delivery-health.json'),true);
    auditCheck($health['status']==='degraded' && $health['pending_alert_type']==='error','Disabled email must leave persisted failure and pending alert');
    $at=new DateTimeImmutable('2026-09-05T00:00:00Z');
    $alert=(new XserverMail\SystemAlertFormatter())->format('error',$at,$at,'input_too_large');
    auditCheck(str_contains($alert['body'],'添付を含む受信メール全体が10MiBの上限を超えたため通知できませんでした。'),'Size alert must explain the limit in Japanese');
    auditCheck(str_contains($alert['body'],'元のメールボックスで確認してください。'),'Size alert must give an actionable recovery path');

    $logger=new OperationalLogger($config['log_path']);$hfs=new AuditMemoryFs();$ofs=new AuditMemoryFs();
    $adapter=new class implements SendmailProcessAdapter {
        public function start(array $argv):SendmailProcessHandle{throw new RuntimeException('mail disabled');}
    };
    $monitor=new DeliveryHealthMonitor($dir.'/state/delivery-health.json',['admin@example.invalid'],$config['log_path'],
        new SystemMailAuthenticator(str_repeat('a',32)),new SendmailClient($adapter),$logger,$hfs,
        fn()=>new DateTimeImmutable('2026-09-05T00:00:00Z'),fn()=>str_repeat('a',32));
    $monitor->recordFailure($monitor->reserveObservation(),'http_error',str_repeat('b',64));
    $outbox=new DeliveryOutbox($dir.'/state/delivery-outbox.json',$ofs);
    $id=hash('sha256','review');$token=$outbox->begin($id,['target@example.invalid'],'title','text');
    $bad=new WebhookClient('https://example.invalid',fn()=>['status'=>400,'body'=>'{"code":400,"description":"missing parameter"}']);
    $outbox->finish($id,$token,$bad->send('title','text'));$row=$outbox->listMetadata()['items'][0];
    $calls=0;
    $client=new WebhookClient('https://example.invalid',function()use(&$calls,$ofs){++$calls;$ofs->fail=true;return ['status'=>200,'body'=>'{"code":200,"description":"success"}'];},32768,null,$monitor);
    $threw=false;try{(new DeliveryRecovery($outbox,NotifierConfig::fromArray($config),$client,$logger,$monitor))->retrySelected($id,$row['revision']);}catch(RuntimeException){$threw=true;}
    auditCheck($threw && $calls===1,'Uncertain storage must not hide uncertainty or resend');
    auditCheck($monitor->status()==='healthy','Confirmed HTTP success must recover health even if outbox finish fails');
    $state=json_decode($hfs->files[$dir.'/state/delivery-health.json'],true);
    auditCheck($state['last_applied_sequence']===2 && $state['pending_alert_type']==='recovery','Delivery truth and pending recovery must survive storage failure');
    auditCheck($outbox->listMetadata()['items'][0]['state']==='in_flight','Uncertain storage must remain non-retryable');
    $ofs->fail=false;
    $failedId=hash('sha256','review-failure');$failedToken=$outbox->begin($failedId,['target@example.invalid'],'title','text');
    $outbox->finish($failedId,$failedToken,$bad->send('title','text'));
    $failedRow=array_values(array_filter($outbox->listMetadata()['items'],fn($item)=>$item['id']===$failedId))[0];
    $calls=0;
    $client=new WebhookClient('https://example.invalid',function()use(&$calls,$ofs){++$calls;$ofs->fail=true;return ['status'=>400,'body'=>'{"code":400,"description":"missing parameter"}'];},32768,null,$monitor);
    $threw=false;try{(new DeliveryRecovery($outbox,NotifierConfig::fromArray($config),$client,$logger,$monitor))->retrySelected($failedId,$failedRow['revision']);}catch(RuntimeException){$threw=true;}
    auditCheck($threw && $calls===1,'Failed delivery with uncertain storage must not resend');
    $state=json_decode($hfs->files[$dir.'/state/delivery-health.json'],true);
    auditCheck($monitor->status()==='degraded' && $state['last_applied_sequence']===3 && $state['pending_alert_type']==='error','Known transport failure must degrade health despite storage failure');
    echo "PASS: oversized input reporting and recovery health independent of storage\n";
} finally {
    foreach(glob($dir.'/state/{*,.*}',GLOB_BRACE)?:[] as $p)if(is_file($p))unlink($p);
    rmdir($dir.'/state');foreach(glob($dir.'/*')?:[] as $p)if(is_file($p))unlink($p);rmdir($dir);
}
