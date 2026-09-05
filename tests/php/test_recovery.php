<?php
declare(strict_types=1);
require dirname(__DIR__,2).'/vendor/autoload.php';
use XserverMail\{DeliveryOutbox,DeliveryRecovery,NativePrivateStateFilesystem,WebhookClient,NotifierConfig};
function verify(bool $ok,string $why):void {if(!$ok)throw new RuntimeException($why);}
verify(class_exists(DeliveryRecovery::class),'Recovery service must exist');
$home=sys_get_temp_dir().'/recovery-'.bin2hex(random_bytes(8));mkdir($home,0700);$home=realpath($home);mkdir($home.'/state',0700);
$outbox=new DeliveryOutbox($home.'/state/delivery-outbox.json',new NativePrivateStateFilesystem(null,null,fn()=>['home'=>$home,'uid'=>posix_geteuid()]));
$id=hash('sha256','mail');$token=$outbox->begin($id,['target@example.invalid'],'saved title','saved text');
$bad=new WebhookClient('https://example.invalid',fn()=>['status'=>400,'body'=>'{"code":400,"description":"missing parameter"}']);
$outbox->finish($id,$token,$bad->send('title','text'));
$config=NotifierConfig::fromArray(['webhook_url'=>'https://webhook.worksmobile.com/message/current',
 'error_recipients'=>['admin@example.invalid'],'notification_targets'=>['target@example.invalid'],
 'notification_pinned_targets'=>[],'system_mail_hmac_key'=>rtrim(base64_encode(str_repeat('a',32)),'='),'log_path'=>$home.'/state/log.json']);
$calls=[];
$client=new WebhookClient($config->webhookUrl,function($url,$payload)use(&$calls){$calls[]=[$url,json_decode($payload,true)];return ['status'=>200,'body'=>'{"code":200,"description":"success"}'];});
$service=new DeliveryRecovery($outbox,$config,$client);
$row=$outbox->listMetadata()['items'][0];
try {
 verify($service->retrySelected($id,$row['revision'])['status']==='delivered','Selected recovery must deliver');
 verify($calls===[['https://webhook.worksmobile.com/message/current',['title'=>'saved title','body'=>['text'=>'saved text']]]],'Must use current URL and exact stored formatted content');
 try {$service->retrySelected($id,$row['revision']);throw new LogicException('Duplicate retry');}catch(RuntimeException){}
 verify(count($calls)===1,'Stale retry must never send');
 $logger=new XserverMail\OperationalLogger($home.'/state/log.json');
 $app=new XserverMail\DeliveryApplication($bad,new XserverMail\ErrorReporter($bad,$logger),$logger,$config,
    null,null,null,null,null,$outbox);
 $raw="From: sender@example.invalid\r\nTo: target@example.invalid\r\nMessage-ID: <recovery-app@example.invalid>\r\nSubject: formatted\r\n\r\nmail body";
 $app->deliver($raw);
 $pending=array_values(array_filter($outbox->listMetadata()['items'],fn($r)=>$r['state']==='pending'));
 verify(count($pending)===1,'Inbound definite rejection must be durably staged');
 $app->deliver($raw);
 verify(count($outbox->listMetadata()['items'])===2,'Outbox claim suppresses repeated inbound message');
 $faultyFs=new class implements XserverMail\PrivateStateFilesystem {
  public function withExclusiveLock(string $p,callable $fn):mixed {throw new RuntimeException('unavailable');}
  public function assertExclusiveLockCurrent():void {}
  public function readRegular(string $p,int $n):?string{return null;}
  public function replaceAtomic(string $p,string $b,int $m):void{}
 };
 $failing=new DeliveryOutbox($home.'/state/unavailable.json',$faultyFs);
 $app=new XserverMail\DeliveryApplication($client,new XserverMail\ErrorReporter($client,$logger),$logger,$config,
   null,null,null,null,null,$failing);
 $app->deliver(str_replace('recovery-app','storage-fail-open',$raw));
 verify(count($calls)===2,'Outbox unavailable must preserve inbound sending');
 verify(str_contains(file_get_contents($home.'/state/log.json'),'outbox_store_failure'),'Recovery coverage loss must be recorded safely');
 echo "PASS: selected recovery current configuration and single claim\n";
}finally{foreach(glob($home.'/state/{*,.*}',GLOB_BRACE)?:[] as $p)if(is_file($p))unlink($p);rmdir($home.'/state');rmdir($home);}
