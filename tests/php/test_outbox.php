<?php
declare(strict_types=1);
require dirname(__DIR__, 2) . '/vendor/autoload.php';
use XserverMail\{DeliveryOutbox,NativePrivateStateFilesystem,WebhookClient};
function check(bool $ok, string $why): void { if (!$ok) throw new RuntimeException($why); }
function rejects(callable $fn): void { try { $fn(); } catch (RuntimeException) { return; } throw new RuntimeException('Expected refusal'); }
check(class_exists(DeliveryOutbox::class), 'Private outbox must exist');
$home = sys_get_temp_dir() . '/outbox-' . bin2hex(random_bytes(8)); mkdir($home,0700); $home=realpath($home);
mkdir($home.'/state',0700); $path=$home.'/state/delivery-outbox.json';
$now=1700000000;
$fs=new NativePrivateStateFilesystem(null,null,fn()=>['home'=>$home,'uid'=>posix_geteuid()]);
$outbox=new DeliveryOutbox($path,$fs,function()use(&$now){return $now;});
$id=hash('sha256','mail');
try {
 $token=$outbox->begin($id,['User@example.invalid'],'private-title','private-text');
 check(is_string($token),'Begin must claim before send');
 check($outbox->begin($id,['User@example.invalid'],'private-title','private-text')===null,'Duplicate must not reclaim');
 rejects(fn()=>$outbox->begin($id,['User@example.invalid'],'different','private-text'));
 $client=new WebhookClient('https://example.invalid',fn()=>['status'=>400,'body'=>'{"code":400,"description":"invalid parameter"}']);
 $outbox->finish($id,$token,$client->send('title','text'));
 $row=$outbox->listMetadata()['items'][0];
 check($row['retryable'] && $row['state']==='pending','Definite total rejection must be replayable');
 check(!str_contains(json_encode($outbox->listMetadata()),'private-') && !str_contains(json_encode($outbox->listMetadata()),'@'),'List must exclude private payload');
 rejects(fn()=>$outbox->claimSelected($id,$row['revision'],['other@example.invalid']));
 $claim=$outbox->claimSelected($id,$row['revision'],['user@example.invalid']);
 check($claim['text']==='private-text','Claim returns exact payload');
 rejects(fn()=>$outbox->claimSelected($id,$row['revision'],['user@example.invalid']));
 $ok=new WebhookClient('https://example.invalid',fn()=>['status'=>200,'body'=>'{"code":200,"description":"success"}']);
 $outbox->finish($id,$claim['token'],$ok->send('title','text'));
 check(!str_contains(file_get_contents($path),'private-text'),'Delivered tombstone removes payload');
 check($outbox->begin($id,['User@example.invalid'],'private-title','private-text')===null,'Tombstone prevents full replay');
 foreach ([
  [['status'=>500,'body'=>'{}'],['status'=>400,'body'=>'{"code":400,"description":"invalid parameter"}']],
  [['status'=>400,'body'=>'not json']],
  [['status'=>400,'body'=>'{"code":400,"description":"limit exceeded (body.text length exceeds 2000)"}'],['status'=>200,'body'=>'{"code":200,"description":"success"}'],['status'=>400,'body'=>'{"code":400,"description":"invalid parameter"}']],
 ] as $n=>$responses) {
  $key=hash('sha256','uncertain'.$n); $t=$outbox->begin($key,['User@example.invalid'],'title',str_repeat('x',4000));
  $client=new WebhookClient('https://example.invalid',function()use(&$responses){return array_shift($responses);},32768,fn()=>null);
  $outbox->finish($key,$t,$client->send('title',str_repeat('x',4000)));
  $rows=$outbox->listMetadata()['items']; $r=array_values(array_filter($rows,fn($r)=>$r['id']===$key))[0];
  check(!$r['retryable'] && $r['state']==='review_only','Ambiguous and partial failures must never replay whole payload');
  rejects(fn()=>$outbox->claimSelected($key,$r['revision'],['user@example.invalid']));
 }
 rejects(fn()=>$outbox->begin(hash('sha256','big'),['User@example.invalid'],'title',str_repeat('x',1048576)));
 $now+=604801;
 foreach($outbox->listMetadata()['items'] as $r) check(!$r['retryable'],'Expired payload cannot replay');
 check(is_string($outbox->begin($id,['User@example.invalid'],'new','text')),'Expired tombstone can be pruned');
 for($i=0;$i<105;$i++) {
  $key=hash('sha256','delivered'.$i);$t=$outbox->begin($key,['User@example.invalid'],'title','text');
  $outbox->finish($key,$t,$ok->send('title','text'));
 }
 check(count($outbox->listMetadata()['items'])===100,'Delivered tombstones must be bounded without blocking new messages');
 $canonical=file_get_contents($path);
 file_put_contents($path,str_replace('"schema_version":1','"schema_version":2,"schema_version":1',$canonical));
 rejects(fn()=>$outbox->listMetadata());
 file_put_contents($path,$canonical);
 $firstRevision=$outbox->listMetadata()['items'][0]['revision'];
 file_put_contents($path,str_replace('"revision":"'.$firstRevision.'"','"revision":"'.str_repeat('f',64).'","revision":"'.$firstRevision.'"',$canonical));
 rejects(fn()=>$outbox->listMetadata());
 file_put_contents($path,'{}'); chmod($path,0600);
 rejects(fn()=>$outbox->listMetadata());
 unlink($path);
 for($i=0;$i<100;$i++)$outbox->begin(hash('sha256','live'.$i),['User@example.invalid'],'title','text');
 $full=file_get_contents($path);
 rejects(fn()=>$outbox->begin(hash('sha256','overflow'),['User@example.invalid'],'title','text'));
 check(file_get_contents($path)===$full,'Capacity refusal must preserve every live claim');
 unlink($path);
 for($i=0;$i<16;$i++)$outbox->begin(hash('sha256','large'.$i),['User@example.invalid'],'title',str_repeat('x',1040000));
 $full=file_get_contents($path);
 rejects(fn()=>$outbox->begin(hash('sha256','store-overflow'),['User@example.invalid'],'title',str_repeat('x',1040000)));
 check(file_get_contents($path)===$full && strlen($full)<=16777216,'Store bound checked before atomic replace');
 unlink($path);
 $fault=false;
 $faultFs=new NativePrivateStateFilesystem(null,function($stage)use(&$fault){if($fault && $stage==='before_replace')throw new RuntimeException('fault');},fn()=>['home'=>$home,'uid'=>posix_geteuid()]);
 $faultBox=new DeliveryOutbox($path,$faultFs,function()use(&$now){return $now;});
 $key=hash('sha256','post-send-failure');$token=$faultBox->begin($key,['User@example.invalid'],'title','text');
 $fault=true;
 rejects(fn()=>$faultBox->finish($key,$token,$ok->send('title','text')));
 $fault=false;$row=$faultBox->listMetadata()['items'][0];
 check(!$row['retryable'] && $row['state']==='in_flight','Failure saving success must never return to pending');
 rejects(fn()=>$faultBox->claimSelected($key,$row['revision'],['User@example.invalid']));
 rename($path,$path.'.actual');symlink($path.'.actual',$path);
 rejects(fn()=>$outbox->listMetadata());unlink($path);link($path.'.actual',$path);
 rejects(fn()=>$outbox->listMetadata());unlink($path);rename($path.'.actual',$path);
 echo "PASS: private outbox safe replay and review-only retention\n";
} finally { foreach(glob($home.'/state/{*,.*}',GLOB_BRACE)?:[] as $file) if(is_file($file))unlink($file); rmdir($home.'/state');rmdir($home); }
