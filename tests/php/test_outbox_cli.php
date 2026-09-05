<?php
declare(strict_types=1);
require dirname(__DIR__,2).'/vendor/autoload.php';
$root=dirname(__DIR__,2);
$home=sys_get_temp_dir().'/outbox-cli-'.bin2hex(random_bytes(8));mkdir($home,0700);$home=realpath($home);mkdir($home.'/state',0700);
// Substitute only account resolution and dangerous constructors, not CLI routing or private storage.
$prelude=$home.'/prelude.php';
file_put_contents($prelude,'<?php namespace XserverMail; function posix_getpwuid($uid){return ["dir"=>getenv("TEST_HOME")];} class WebhookClient {public function __construct(){file_put_contents(getenv("TEST_HOME")."/danger","webhook");throw new \\RuntimeException();}} class ErrorReporter {public function __construct(){file_put_contents(getenv("TEST_HOME")."/danger","reporter");throw new \\RuntimeException();}}');
$config=['webhook_url'=>'https://webhook.worksmobile.com/message/test','error_recipients'=>['admin@example.invalid'],
 'notification_targets'=>['target@example.invalid'],'notification_pinned_targets'=>[],
 'system_mail_hmac_key'=>rtrim(base64_encode(str_repeat('a',32)),'='),'log_path'=>$home.'/state/log.json'];
$json=json_encode($config);$frame=XserverMail\StdinFrame::MAGIC.pack('NN',0,strlen($json)).$json;
function runCli(array $args,string $frame):array{global $home,$root,$prelude;$pipes=[];$p=proc_open([PHP_BINARY,'-d','auto_prepend_file='.$prelude,$root.'/bin/mail-to-lineworks.php',...$args],[['pipe','r'],['pipe','w'],['pipe','w']],$pipes,null,['TEST_HOME'=>$home,'MAIL_NOTIFIER_STDIN_FRAME'=>'1']);fwrite($pipes[0],$frame);fclose($pipes[0]);$out=stream_get_contents($pipes[1]);fclose($pipes[1]);$err=stream_get_contents($pipes[2]);fclose($pipes[2]);return[proc_close($p),$out,$err];}
try {
 [$code,$out]=runCli(['--outbox-list'],$frame);
 if($code!==0 || json_decode($out,true)!==['schema_version'=>1,'items'=>[]])throw new RuntimeException('List must run without constructing notifier');
 foreach([['--check-config'],['--check-message']] as $args)if(runCli($args,$frame)[0]!==0)throw new RuntimeException('Checks must not construct notifier');
 file_put_contents($home.'/state/delivery-outbox.json','{}');chmod($home.'/state/delivery-outbox.json',0600);
 if(runCli(['--outbox-list'],$frame)[0]===0)throw new RuntimeException('Corrupt list must fail closed');
 if(is_file($home.'/danger'))throw new RuntimeException('Read-only modes instantiated notifier or reporter');
 foreach([['--outbox-list','extra'],['--outbox-retry'],['--outbox-retry',str_repeat('A',64),str_repeat('b',64)]] as $args)if(runCli($args,$frame)[0]===0)throw new RuntimeException('Invalid CLI shape accepted');
 echo "PASS: recovery CLI read-only paths never instantiate notification clients\n";
} finally {foreach(glob($home.'/state/{*,.*}',GLOB_BRACE)?:[] as $p)if(is_file($p))unlink($p);rmdir($home.'/state');foreach(glob($home.'/*')?:[] as $p)unlink($p);rmdir($home);}
