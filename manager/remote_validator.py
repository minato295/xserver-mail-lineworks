"""Fail-closed SSH validation without retaining expanded connection details."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import subprocess
import selectors
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


class RemoteValidationError(RuntimeError):
    """A fixed, non-secret remote-validation failure."""


def fixed_runtime_php(trusted_root="/home", trusted_uid=0):
    """Generate the data-independent system-PHP inspector for production/tests."""
    if (not isinstance(trusted_root, str) or not trusted_root.startswith("/")
            or trusted_root == "/" or "public_html" in trusted_root.casefold()
            or type(trusted_uid) is not int or trusted_uid < 0):
        raise ValueError("trusted root policy is invalid")
    template = r'''$r=json_decode(stream_get_contents(STDIN),true,16,JSON_THROW_ON_ERROR);$p=$r["root"]??"";$e=$r["entries"]??null;$t=__TRUSTED__;$u=__UID__;if(!is_string($p)||!str_starts_with($p,$t."/")||stripos($p,"public_html")!==false||!is_array($e))throw new Exception();$z=lstat("/");$h=lstat($t);if($z===false||($z["mode"]&0170000)!==0040000||is_link("/")||$z["uid"]!==0||($z["mode"]&0022)!==0||$h===false||($h["mode"]&0170000)!==0040000||is_link($t)||$h["uid"]!==$u||($h["mode"]&0022)!==0)throw new Exception();$c=$t;foreach(explode("/",substr($p,strlen($t)+1)) as $i=>$x){$c.="/".$x;$v=file_exists($c)||is_link($c);if($i===0&&!$v)throw new Exception();if($v){$s=lstat($c);$m=$s===false?-1:$s["mode"]&0777;$ok=$i===0?($m===0700||$m===0701):$m===0700;if($s===false||($s["mode"]&0170000)!==0040000||is_link($c)||$s["uid"]!==posix_geteuid()||!$ok)throw new Exception();}}if(!file_exists($p)){echo "{\"state\":\"ABSENT\",\"present_files\":[]}\n";exit;}$a=[];$w=["deploy-transactions"=>1,"logs"=>1,"releases"=>1,"state"=>1];$di=new RecursiveDirectoryIterator($p,FilesystemIterator::SKIP_DOTS);$fi=new RecursiveCallbackFilterIterator($di,function($f)use($p,$w){$q=$f->getPathname();$n=substr($q,strlen($p)+1);if(strpos($n,"/")===false&&isset($w[$n])){$s=lstat($q);if($s===false||($s["mode"]&0170000)!==0040000||is_link($q)||$s["uid"]!==posix_geteuid()||($s["mode"]&0777)!==0700)throw new Exception();return false;}return true;});$it=new RecursiveIteratorIterator($fi,RecursiveIteratorIterator::SELF_FIRST);foreach($it as $f){$q=$f->getPathname();$s=lstat($q);if($s===false||is_link($q)||$s["uid"]!==posix_geteuid())throw new Exception();$n=substr($q,strlen($p)+1);$d=is_dir($q);$a[$n]=["mode"=>$s["mode"]&0777,"sha256"=>$d?null:hash_file("sha256",$q),"size"=>$d?0:$s["size"],"type"=>$d?"directory":"file"];}ksort($a,SORT_STRING);ksort($e,SORT_STRING);$files=[];foreach($a as $k=>$v)if($v["type"]==="file")$files[]=$k;if($a===$e){echo json_encode(["state"=>"EXACT","present_files"=>$files],JSON_UNESCAPED_SLASHES)."\n";exit;}foreach($a as $k=>$v)if(!isset($e[$k])||$e[$k]!==$v)throw new Exception();echo json_encode(["state"=>"PARTIAL","present_files"=>$files],JSON_UNESCAPED_SLASHES)."\n";'''
    return template.replace("__TRUSTED__", json.dumps(trusted_root)).replace("__UID__", str(trusted_uid))


def fixed_helper_php(trusted_root="/home", trusted_uid=0):
    """Generate the pinned helper-only writer used before the helper exists."""
    if (not isinstance(trusted_root, str) or not trusted_root.startswith("/")
            or trusted_root == "/" or "public_html" in trusted_root.casefold()
            or type(trusted_uid) is not int or trusted_uid < 0):
        raise ValueError("trusted root policy is invalid")
    template = r'''$x=stream_get_contents(STDIN,131073);if(!is_string($x)||strlen($x)>131072)throw new Exception();$r=json_decode($x,true,16,JSON_THROW_ON_ERROR);$k=["schema_version","root","relative","body_base64","sha256","mode"];if(!is_array($r)||count($r)!==6||array_diff($k,array_keys($r))||array_diff(array_keys($r),$k)||($r["schema_version"]??null)!==1)throw new Exception();$p=$r["root"]??null;$n=$r["relative"]??null;$b64=$r["body_base64"]??null;$sha=$r["sha256"]??null;$mode=$r["mode"]??null;$t=__TRUSTED__;$u=__UID__;if(!is_string($p)||!str_starts_with($p,$t."/")||stripos($p,"public_html")!==false||$n!=="bootstrap/manage-private-config.php"||!is_string($b64)||!is_string($sha)||preg_match('/\A[a-f0-9]{64}\z/D',$sha)!==1||$mode!==0700)throw new Exception();$q=explode("/",substr($p,strlen($t)+1));if(count($q)!==3||preg_match('/\A[A-Za-z0-9._-]+\z/D',$q[0])!==1||in_array($q[0],[".",".."],true)||$q[1]!=="private"||$q[2]!=="xserver-mail-lineworks")throw new Exception();$body=base64_decode($b64,true);if(!is_string($body)||strlen($body)<1||strlen($body)>65536||!hash_equals($sha,hash("sha256",$body)))throw new Exception();$z=lstat("/");$h=lstat($t);if($z===false||($z["mode"]&0170000)!==0040000||is_link("/")||$z["uid"]!==0||($z["mode"]&0022)!==0||$h===false||($h["mode"]&0170000)!==0040000||is_link($t)||$h["uid"]!==$u||($h["mode"]&0022)!==0)throw new Exception();$c=$t;foreach($q as $i=>$part){$c.="/".$part;$s=lstat($c);$m=$s===false?-1:$s["mode"]&0777;$ok=$i===0?($m===0700||$m===0701):$m===0700;if($s===false||($s["mode"]&0170000)!==0040000||is_link($c)||$s["uid"]!==posix_geteuid()||!$ok)throw new Exception();}$parent=$p."/bootstrap";$ps=lstat($parent);if($ps===false||($ps["mode"]&0170000)!==0040000||is_link($parent)||$ps["uid"]!==posix_geteuid()||($ps["mode"]&0777)!==0700)throw new Exception();$target=$parent."/manage-private-config.php";$exact=function($path)use($sha,$body){$s=lstat($path);return $s!==false&&($s["mode"]&0170000)===0100000&&!is_link($path)&&$s["uid"]===posix_geteuid()&&($s["mode"]&0777)===0700&&$s["size"]===strlen($body)&&hash_equals($sha,hash_file("sha256",$path));};if(file_exists($target)||is_link($target)){if(!$exact($target))throw new Exception();echo "{\"status\":\"unchanged\"}\n";exit;}$old=umask(0077);$tmp=$parent."/.manage-private-config.".bin2hex(random_bytes(16)).".tmp";$f=@fopen($tmp,"x+b");umask($old);if($f===false)throw new Exception();try{$off=0;$len=strlen($body);while($off<$len){$w=fwrite($f,substr($body,$off));if($w===false||$w<1)throw new Exception();$off+=$w;}if(!fflush($f)||!chmod($tmp,0700))throw new Exception();$a=fstat($f);if($a===false||($a["mode"]&0170000)!==0100000||$a["uid"]!==posix_geteuid()||($a["mode"]&0777)!==0700||$a["size"]!==$len||!rewind($f))throw new Exception();$ctx=hash_init("sha256");$read=0;while(!feof($f)){$chunk=fread($f,min(65536,$len+1-$read));if($chunk===false)throw new Exception();if($chunk==="")break;$read+=strlen($chunk);if($read>$len)throw new Exception();hash_update($ctx,$chunk);}if($read!==$len||!hash_equals($sha,hash_final($ctx)))throw new Exception();$a2=fstat($f);if($a2===false||$a2["dev"]!==$a["dev"]||$a2["ino"]!==$a["ino"]||$a2["uid"]!==$a["uid"]||($a2["mode"]&0777)!==0700||$a2["size"]!==$a["size"])throw new Exception();if(!fclose($f))throw new Exception();$f=null;if(file_exists($target)||is_link($target)){if(!$exact($target))throw new Exception();@unlink($tmp);echo "{\"status\":\"unchanged\"}\n";exit;}if(!@rename($tmp,$target))throw new Exception();$tmp=null;$final=lstat($target);if($final===false||($final["mode"]&0170000)!==0100000||is_link($target)||$final["uid"]!==posix_geteuid()||($final["mode"]&0777)!==0700||$final["size"]!==$len||$final["dev"]!==$a["dev"]||$final["ino"]!==$a["ino"]||!hash_equals($sha,hash_file("sha256",$target)))throw new Exception();echo "{\"status\":\"changed\"}\n";}catch(Throwable $e){if(is_resource($f))fclose($f);if(is_string($tmp))@unlink($tmp);throw new Exception();}'''
    return template.replace("__TRUSTED__", json.dumps(trusted_root)).replace("__UID__", str(trusted_uid))


def bounded_subprocess_run(argv, *, input=None, timeout=30, stdout_limit=65536,
                           stderr_limit=65536):
    """Run a fixed argv while bounding time and captured bytes."""
    if (not isinstance(argv, list) or not argv or any(type(item) is not str for item in argv)
            or input is not None and type(input) is not bytes
            or any(type(value) is not int or value < 0 for value in
                   (timeout, stdout_limit, stderr_limit))):
        raise ValueError("subprocess contract is invalid")
    stdin_file = tempfile.TemporaryFile()
    if input:
        stdin_file.write(input)
    stdin_file.seek(0)
    process = subprocess.Popen(argv, stdin=stdin_file, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False, close_fds=True)
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout_limit))
        selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr_limit))
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in events:
                name, limit = key.data
                chunk = os.read(key.fileobj.fileno(), min(65536, limit + 1 - len(captured[name])))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured[name].extend(chunk)
                if len(captured[name]) > limit:
                    raise RemoteValidationError("SSH応答が大きすぎます。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(argv, returncode, bytes(captured["stdout"]),
                                           bytes(captured["stderr"]))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
        stdin_file.close()


@dataclass(frozen=True)
class ValidationResult:
    manifest: str
    php_cli: str
    absolute_cli_dry_run: str
    symlinks: tuple
    public_root: str


@dataclass(frozen=True)
class _TrustSnapshot:
    device: int
    inode: int
    uid: int
    mode: int
    size: int
    sha256: str


def _logical_lines(text: str) -> Iterable[str]:
    current = ""
    for physical in text.splitlines(keepends=True):
        line = physical.rstrip("\r\n")
        trailing = 0
        for character in reversed(line):
            if character != "\\":
                break
            trailing += 1
        if trailing % 2:
            current += line[:-1]
            continue
        yield current + line
        current = ""
    if current:
        raise RemoteValidationError("SSH設定を安全に解釈できません。")


def _strip_comment(line: str) -> str:
    result = []
    quote = None
    escaped = False
    for character in line:
        if escaped:
            result.append(character)
            escaped = False
        elif character == "\\":
            result.append(character)
            escaped = True
        elif quote:
            result.append(character)
            if character == quote:
                quote = None
        elif character in "'\"":
            result.append(character)
            quote = character
        elif character == "#":
            break
        else:
            result.append(character)
    if quote or escaped:
        raise RemoteValidationError("SSH設定を安全に解釈できません。")
    return "".join(result)


def parse_ssh_config(data: bytes, selected_alias: str) -> str:
    """Accept only a static, exact, single-pattern Host stanza."""
    if len(data) > 65536 or b"\0" in data:
        raise RemoteValidationError("SSH設定を安全に解釈できません。")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RemoteValidationError("SSH設定を安全に解釈できません。") from error

    selected = 0
    for logical in _logical_lines(text):
        clean = _strip_comment(logical).strip()
        if not clean:
            continue
        try:
            tokens = shlex.split(clean, comments=False, posix=True)
        except ValueError as error:
            raise RemoteValidationError("SSH設定を安全に解釈できません。") from error
        if not tokens or not tokens[0].isascii() or not tokens[0].isalpha():
            raise RemoteValidationError("SSH設定を安全に解釈できません。")
        directive = tokens[0].casefold()
        if directive in {"include", "match", "hostkeyalias"}:
            raise RemoteValidationError("SSH設定に利用できない項目があります。")
        if directive == "host":
            # Quoting/escaping a pattern is unnecessary and creates ambiguity.
            raw_arguments = clean[len(tokens[0]):].strip()
            if len(tokens) != 2 or any(mark in raw_arguments for mark in "'\"\\"):
                raise RemoteValidationError("SSH接続名は単独の完全一致で指定してください。")
            pattern = tokens[1]
            if any(mark in pattern for mark in "*!?"):
                raise RemoteValidationError("SSH接続名は単独の完全一致で指定してください。")
            if pattern == selected_alias:
                selected += 1
    if selected != 1:
        raise RemoteValidationError("SSH接続名を一意に確認できません。")
    return selected_alias


class RemoteValidator:
    SSH = "/usr/bin/ssh"
    CONFIG_BASENAME = "xserver-mail-lineworks.conf"
    REMOTE_COMMAND = "/usr/bin/php8.5 private/xserver-mail-lineworks/bootstrap/validate-release.php"
    _FIXED_RUNTIME_PHP = fixed_runtime_php()
    FIXED_RUNTIME_COMMAND = "/usr/bin/php8.5 -r " + shlex.quote(_FIXED_RUNTIME_PHP)
    _FIXED_HELPER_PHP = fixed_helper_php()
    FIXED_HELPER_COMMAND = "/usr/bin/php8.5 -r " + shlex.quote(_FIXED_HELPER_PHP)
    OUTPUT_LIMIT = 65536
    TRUSTED_INPUT_LIMIT = 262144
    TRUSTED_OUTPUT_LIMIT = 131072
    TIMEOUT = 30
    OPTIONS = (
        "BatchMode=yes", "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=%d/.ssh/known_hosts", "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no", "PermitLocalCommand=no", "RemoteCommand=none",
        "ProxyCommand=none", "ProxyJump=none", "KnownHostsCommand=none",
        "CanonicalizeHostname=no", "ClearAllForwardings=yes", "ForwardAgent=no",
        "ControlMaster=no", "ControlPath=none", "RequestTTY=no", "LogLevel=ERROR",
    )
    REQUIRED_G = {
        "hostkeyalias": {"", "none"}, "proxycommand": {"", "none"},
        "proxyjump": {"", "none"}, "knownhostscommand": {"", "none"},
        "remotecommand": {"", "none"},
        "canonicalizehostname": {"no", "false"}, "clearallforwardings": {"yes"},
        "forwardagent": {"no", "false"}, "controlmaster": {"no", "false"},
        "controlpath": {"", "none"}, "permitlocalcommand": {"no", "false"},
        "requesttty": {"no", "false"},
    }
    REPEATABLE_G = {"identityfile", "sendenv"}
    RESULT_KEYS = {"manifest", "php_cli", "absolute_cli_dry_run", "symlinks", "public_root"}
    RELEASE_RESULT_KEYS = {
        "schema_version", "manifest", "php_cli", "autoload",
        "absolute_cli_dry_run", "symlinks",
    }
    AUDIT_RESULT_KEYS = {
        "schema_version", "home_symlink", "home_mode", "public_roots_scanned",
        "symlinks", "known_product_matches", "untrusted_subtrees", "untrusted_entries",
    }

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def __init__(self, ssh_alias: str, runner: Callable | None = None, *, home: Path | None = None):
        if (not isinstance(ssh_alias, str) or not ssh_alias.isascii() or not ssh_alias
                or ssh_alias.startswith("-") or any(not (c.isalnum() or c in "._-") for c in ssh_alias)):
            raise ValueError("ssh_alias is invalid")
        self.ssh_alias = ssh_alias
        self.runner = runner or bounded_subprocess_run
        self.home = Path(home) if home is not None else Path.home()
        self.ssh_dir = self.home / ".ssh"
        self.config = self.ssh_dir / self.CONFIG_BASENAME
        self.known_hosts = self.ssh_dir / "known_hosts"

    def _base_argv(self):
        argv = [self.SSH, "-F", str(self.config)]
        for option in self.OPTIONS:
            argv.extend(("-o", option))
        return argv

    def inspection_argv(self, remote_command=None):
        return [*self._base_argv()[:3], "-G", *self._base_argv()[3:], "--", self.ssh_alias,
                self.REMOTE_COMMAND if remote_command is None else remote_command]

    def actual_argv(self):
        return [*self._base_argv(), "--", self.ssh_alias, self.REMOTE_COMMAND]

    @staticmethod
    def _snapshot(path: Path, expected_mode: int, *, directory=False) -> _TrustSnapshot:
        try:
            before = path.lstat()
        except OSError as error:
            raise RemoteValidationError("SSH信頼設定を確認できません。") from error
        if stat.S_ISLNK(before.st_mode) or (directory != stat.S_ISDIR(before.st_mode)):
            raise RemoteValidationError("SSH信頼設定を確認できません。")
        if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != expected_mode:
            raise RemoteValidationError("SSH信頼設定の権限が安全ではありません。")
        if directory:
            digest = ""
        else:
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise RemoteValidationError("SSH信頼設定が変更されました。")
                    hasher = hashlib.sha256()
                    total = 0
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 1024 * 1024:
                            raise RemoteValidationError("SSH信頼設定が大きすぎます。")
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
                finally:
                    os.close(fd)
            except OSError as error:
                raise RemoteValidationError("SSH信頼設定を確認できません。") from error
        return _TrustSnapshot(before.st_dev, before.st_ino, before.st_uid,
                              stat.S_IMODE(before.st_mode), before.st_size, digest)

    def _trust(self):
        directory = self._snapshot(self.ssh_dir, 0o700, directory=True)
        config = self._snapshot(self.config, 0o600)
        known = self._snapshot(self.known_hosts, 0o600)
        return directory, config, known

    def _assert_same_trust(self, expected):
        if self._trust() != expected:
            raise RemoteValidationError("SSH信頼設定が変更されました。")

    @staticmethod
    def _read_verified(path: Path, expected: _TrustSnapshot, limit: int) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if ((opened.st_dev, opened.st_ino, opened.st_uid,
                     stat.S_IMODE(opened.st_mode), opened.st_size)
                        != (expected.device, expected.inode, expected.uid,
                            expected.mode, expected.size)):
                    raise RemoteValidationError("SSH信頼設定が変更されました。")
                chunks = []
                total = 0
                while True:
                    chunk = os.read(fd, min(65536, limit + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limit:
                        raise RemoteValidationError("SSH設定が大きすぎます。")
                data = b"".join(chunks)
                if hashlib.sha256(data).hexdigest() != expected.sha256:
                    raise RemoteValidationError("SSH信頼設定が変更されました。")
                return data
            finally:
                os.close(fd)
        except OSError as error:
            raise RemoteValidationError("SSH信頼設定を確認できません。") from error

    def _run(self, argv, input_data):
        return self._run_bounded(argv, input_data, self.OUTPUT_LIMIT)

    def _run_bounded(self, argv, input_data, output_limit):
        try:
            result = self.runner(argv, input=input_data, timeout=self.TIMEOUT,
                                 stdout_limit=output_limit, stderr_limit=output_limit)
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RemoteValidationError("SSH処理が時間内に完了しませんでした。") from error
        if result.returncode != 0:
            raise RemoteValidationError("SSH処理に失敗しました。")
        if len(result.stdout) > output_limit or len(result.stderr) > output_limit:
            raise RemoteValidationError("SSH応答が大きすぎます。")
        return result.stdout

    def run_trusted(self, remote_command: str, input_data: bytes, *, expected_hosts: list[str],
                    output_limit: int = TRUSTED_OUTPUT_LIMIT) -> bytes:
        """Run caller-selected fixed code through the complete SSH trust boundary."""
        if (type(remote_command) is not str or not remote_command or "\0" in remote_command
                or type(input_data) is not bytes
                or len(input_data) > self.TRUSTED_INPUT_LIMIT
                or type(output_limit) is not int
                or not 1 <= output_limit <= self.TRUSTED_OUTPUT_LIMIT
                or not isinstance(expected_hosts, (list, tuple)) or not expected_hosts
                or any(type(host) is not str or not host for host in expected_hosts)):
            raise ValueError("trusted SSH contract is invalid")
        trust = self._trust()
        config_bytes = self._read_verified(self.config, trust[1], self.OUTPUT_LIMIT)
        parse_ssh_config(config_bytes, self.ssh_alias)
        self._assert_same_trust(trust)
        resolved = self._run_bounded(self.inspection_argv(remote_command), None,
                                     self.OUTPUT_LIMIT)
        self._parse_g(resolved, expected_hosts)
        self._assert_same_trust(trust)
        output = self._run_bounded(
            [*self._base_argv(), "--", self.ssh_alias, remote_command], input_data, output_limit,
        )
        self._assert_same_trust(trust)
        return output

    def _parse_g(self, output: bytes, expected_hosts):
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RemoteValidationError("SSH設定の解決結果を確認できません。") from error
        values = {}
        for line in text.splitlines():
            if not line or " " not in line:
                raise RemoteValidationError("SSH設定の解決結果を確認できません。")
            key, value = line.split(" ", 1)
            key = key.casefold()
            if key in values and key not in self.REPEATABLE_G:
                raise RemoteValidationError("SSH設定の解決結果を確認できません。")
            if key not in self.REPEATABLE_G:
                values[key] = value.strip()
        if values.get("hostname") not in set(expected_hosts):
            raise RemoteValidationError("SSH接続先を照合できません。")
        expected_known = str(self.known_hosts)
        if values.get("userknownhostsfile") != expected_known:
            raise RemoteValidationError("known_hostsを固定できません。")
        for key, allowed in self.REQUIRED_G.items():
            if values.get(key, "").casefold() not in allowed:
                raise RemoteValidationError("SSH設定に危険な項目があります。")
        if values.get("localcommand", "none").casefold() != "none":
            raise RemoteValidationError("SSH設定に危険な項目があります。")
        if any(key in values for key in ("localforward", "remoteforward", "dynamicforward")):
            raise RemoteValidationError("SSH設定に危険な項目があります。")

    def validate(self, local_manifest, *, expected_hosts):
        if not expected_hosts or any(not isinstance(host, str) or not host for host in expected_hosts):
            raise ValueError("expected_hosts is invalid")
        trust = self._trust()
        config_bytes = self._read_verified(self.config, trust[1], self.OUTPUT_LIMIT)
        parse_ssh_config(config_bytes, self.ssh_alias)
        self._assert_same_trust(trust)
        resolved = self._run(self.inspection_argv(), None)
        self._parse_g(resolved, expected_hosts)
        self._assert_same_trust(trust)
        payload = json.dumps({"manifest": local_manifest}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.OUTPUT_LIMIT:
            raise RemoteValidationError("検証データが大きすぎます。")
        output = self._run(self.actual_argv(), payload)
        try:
            decoded = json.loads(output.decode("utf-8"), object_pairs_hook=self._unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RemoteValidationError("検証結果を確認できません。") from error
        if not isinstance(decoded, dict) or set(decoded) != self.RESULT_KEYS:
            raise RemoteValidationError("検証結果を確認できません。")
        if (decoded["manifest"] != "PASS" or decoded["php_cli"] != "PASS"
                or decoded["absolute_cli_dry_run"] != "PASS"
                or decoded["public_root"] != "PASS" or decoded["symlinks"] != []):
            raise RemoteValidationError("リモート検証に失敗しました。")
        return ValidationResult(decoded["manifest"], decoded["php_cli"],
                                decoded["absolute_cli_dry_run"], tuple(decoded["symlinks"]),
                                decoded["public_root"])

    def validate_release(self, local_manifest, *, remote_root, entrypoint, config_path,
                         server_home, expected_hosts, known_basenames):
        """Validate a staged release and a metadata-only public-root audit."""
        if (not isinstance(remote_root, str) or not isinstance(entrypoint, str)
                or not isinstance(config_path, str)
                or not isinstance(server_home, str) or not isinstance(known_basenames, list)):
            raise ValueError("release validation context is invalid")
        trust = self._trust()
        config_bytes = self._read_verified(self.config, trust[1], self.OUTPUT_LIMIT)
        parse_ssh_config(config_bytes, self.ssh_alias)
        self._assert_same_trust(trust)
        resolved = self._run(self.inspection_argv(), None)
        self._parse_g(resolved, expected_hosts)
        self._assert_same_trust(trust)

        request = {
            "schema_version": 1, "release_path": remote_root,
            "entrypoint": entrypoint, "config_path": config_path, "manifest": local_manifest,
        }
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise RemoteValidationError("検証データが大きすぎます。")
        release_output = self._run(self.actual_argv(), payload)
        release = self._decode_exact(release_output, self.RELEASE_RESULT_KEYS)
        if (release.get("schema_version") != 1
                or any(release.get(key) != "PASS" for key in (
                    "manifest", "php_cli", "autoload", "absolute_cli_dry_run"))
                or release.get("symlinks") != 0):
            raise RemoteValidationError("リモート検証に失敗しました。")

        self._assert_same_trust(trust)
        audit_payload = json.dumps(
            {"server_home": server_home, "known_basenames": known_basenames},
            ensure_ascii=False, sort_keys=False, separators=(",", ":"),
        ).encode("utf-8")
        audit_command = self.REMOTE_COMMAND + " --audit-public-root"
        audit_output = self._run([*self._base_argv(), "--", self.ssh_alias, audit_command], audit_payload)
        audit = self._decode_exact(audit_output, self.AUDIT_RESULT_KEYS)
        if (audit.get("schema_version") != 3 or audit.get("home_symlink") is not False
                or type(audit.get("home_mode")) is not int
                or not 0 <= audit["home_mode"] <= 0o777
                or audit["home_mode"] & 0o022
                or type(audit.get("public_roots_scanned")) is not int
                or not 0 <= audit["public_roots_scanned"] <= 10000
                or type(audit.get("symlinks")) is not int
                or not 0 <= audit["symlinks"] <= 10000
                or type(audit.get("known_product_matches")) is not int
                or audit["known_product_matches"] != 0
                or type(audit.get("untrusted_subtrees")) is not int
                or not 0 <= audit["untrusted_subtrees"] <= 10000
                or type(audit.get("untrusted_entries")) is not int
                or not 0 <= audit["untrusted_entries"] <= 10000):
            raise RemoteValidationError("公開領域の監査に失敗しました。")
        return {
            "manifest": "PASS", "php_cli": "PASS", "autoload": "PASS",
            "absolute_cli_dry_run": "PASS", "symlinks": 0, "public_root": "PASS",
            "untrusted_subtrees": audit["untrusted_subtrees"],
            "untrusted_entries": audit["untrusted_entries"],
        }

    def inspect_fixed_runtime_details(self, remote_root, entries, *, expected_hosts):
        """Inspect a fixed tree and return its strict state and present files."""
        trust = self._trust()
        config_bytes = self._read_verified(self.config, trust[1], self.OUTPUT_LIMIT)
        parse_ssh_config(config_bytes, self.ssh_alias)
        self._assert_same_trust(trust)
        self._parse_g(self._run(self.inspection_argv(), None), expected_hosts)
        self._assert_same_trust(trust)
        payload = json.dumps({"root": remote_root, "entries": entries}, sort_keys=True,
                             separators=(",", ":")).encode()
        output = self._run([*self._base_argv(), "--", self.ssh_alias,
                            self.FIXED_RUNTIME_COMMAND], payload)
        decoded = self._decode_exact(output, {"state", "present_files"})
        state = decoded["state"]
        present = decoded["present_files"]
        expected_files = tuple(sorted(
            relative for relative, item in entries.items()
            if isinstance(item, dict) and item.get("type") == "file"
        ))
        if (state not in ("ABSENT", "PARTIAL", "EXACT")
                or not isinstance(present, list)
                or any(type(relative) is not str for relative in present)
                or present != sorted(set(present))
                or not set(present).issubset(expected_files)
                or state == "ABSENT" and present
                or state == "EXACT" and tuple(present) != expected_files):
            raise RemoteValidationError("固定runtimeを検証できません。")
        return {"state": state, "present_files": tuple(present)}

    def inspect_fixed_runtime(self, remote_root, entries, *, expected_hosts):
        """Inspect a fixed tree using only system PHP, independent of that tree."""
        return self.inspect_fixed_runtime_details(
            remote_root, entries, expected_hosts=expected_hosts)["state"]

    def provision_fixed_helper(self, remote_root, relative, body, *, expected_sha256,
                               mode, expected_hosts):
        """Provision only the pinned missing helper through the trusted SSH boundary."""
        if (type(remote_root) is not str
                or re.fullmatch(r"/home/(?!\.{1,2}/)[A-Za-z0-9._-]+/private/xserver-mail-lineworks",
                                remote_root) is None
                or relative != "bootstrap/manage-private-config.php"
                or type(body) is not bytes or not 1 <= len(body) <= 65536
                or type(expected_sha256) is not str
                or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
                or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected_sha256)
                or mode != 0o700):
            raise ValueError("fixed helper provision contract is invalid")
        payload = json.dumps({
            "schema_version": 1, "root": remote_root, "relative": relative,
            "body_base64": base64.b64encode(body).decode("ascii"),
            "sha256": expected_sha256, "mode": mode,
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
        output = self.run_trusted(
            self.FIXED_HELPER_COMMAND, payload, expected_hosts=expected_hosts,
            output_limit=1024,
        )
        decoded = self._decode_exact(output, {"status"})
        if decoded["status"] not in {"changed", "unchanged"}:
            raise RemoteValidationError("固定helperを配備できません。")
        return decoded["status"]

    def _decode_exact(self, output, keys):
        try:
            decoded = json.loads(output.decode("utf-8"), object_pairs_hook=self._unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RemoteValidationError("検証結果を確認できません。") from error
        if not isinstance(decoded, dict) or set(decoded) != keys:
            raise RemoteValidationError("検証結果を確認できません。")
        return decoded
