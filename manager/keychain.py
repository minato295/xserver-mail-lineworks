"""Read deployment credentials from macOS Keychain without logging them."""

import os
import re
import stat
import subprocess


class Keychain:
    def __init__(self, api_host="api.xserver.ne.jp", ftps_host="ftps.xserver.ne.jp",
                 *, keychain_path=None):
        self.api_host = api_host
        self.ftps_host = ftps_host
        self._keychain_path = self._validate_keychain_path(keychain_path)

    @staticmethod
    def _validate_keychain_path(path):
        if path is None:
            return None
        if not isinstance(path, str) or not os.path.isabs(path) or not path.endswith(".keychain-db"):
            raise ValueError("一時キーチェーンのパスが不正です")
        try:
            info = os.lstat(path)
        except OSError:
            raise ValueError("一時キーチェーンのパスが不正です") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("一時キーチェーンのパスが不正です")
        return path

    def _arguments(self, arguments):
        if self._keychain_path is not None:
            return [*arguments, self._keychain_path]
        return arguments

    @staticmethod
    def _run(arguments):
        try:
            return subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            raise RuntimeError("macOSキーチェーンを読み取れません") from None

    def _read(self, host, account, protocol, port):
        result = self._run(self._arguments([
            "security", "find-internet-password", "-s", host, "-a", account,
            "-r", protocol, "-P", str(port), "-w",
        ]))
        return result.stdout.rstrip("\r\n")

    def _find_account(self, host, protocol, port):
        result = self._run(self._arguments([
            "security", "find-internet-password", "-s", host,
            "-r", protocol, "-P", str(port),
        ]))
        account_lines = [
            line for line in result.stdout.splitlines()
            if re.match(r'^\s*"acct"', line)
        ]
        if len(account_lines) != 1:
            raise RuntimeError("FTPSキーチェーン項目を一意に特定できません")
        account = re.fullmatch(r'\s*"acct"<blob>="([^"\r\n]+)"\s*', account_lines[0])
        if account is None:
            raise RuntimeError("FTPSキーチェーン項目を一意に特定できません")
        return account.group(1)

    def read_api_key(self):
        return self._read(self.api_host, "Bearer", "htps", 443)

    def read_ftps_credentials(self):
        account = self._find_account(self.ftps_host, "ftps", 21)
        return account, self._read(self.ftps_host, account, "ftps", 21)
