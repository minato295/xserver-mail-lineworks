"""Explicit-FTPS deployment to a private (non-public_html) tree."""

import io
import hashlib
import json
import os
import posixpath
import ssl
import stat
import uuid
import re
from ftplib import FTP_TLS, all_errors, error_perm
from pathlib import Path, PurePosixPath


_MACOS_CA_FILE = "/etc/ssl/cert.pem"
_TRUSTED_CA_OWNER_UID = 0
_MAX_CA_FILE_SIZE = 4 * 1024 * 1024
_PEM_CERTIFICATE_MARKER = b"-----BEGIN CERTIFICATE-----"


def _validate_ca_file(path):
    """Validate one system-owned CA bundle without following a symlink."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise RuntimeError("CA証明書を安全に読み込めません")
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_uid != _TRUSTED_CA_OWNER_UID
                or before.st_mode & 0o022
                or before.st_size < 1
                or before.st_size > _MAX_CA_FILE_SIZE):
            raise RuntimeError("CA証明書を安全に読み込めません")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != _TRUSTED_CA_OWNER_UID
                    or opened.st_mode & 0o022
                    or opened.st_size < 1
                    or opened.st_size > _MAX_CA_FILE_SIZE):
                raise RuntimeError("CA証明書を安全に読み込めません")
            body = bytearray()
            while len(body) <= _MAX_CA_FILE_SIZE:
                chunk = os.read(descriptor, min(65536, _MAX_CA_FILE_SIZE + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            after = os.fstat(descriptor)
            if ((after.st_dev, after.st_ino, after.st_size)
                    != (opened.st_dev, opened.st_ino, opened.st_size)):
                raise RuntimeError("CA証明書を安全に読み込めません")
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError):
        raise RuntimeError("CA証明書を安全に読み込めません") from None
    if (len(body) != before.st_size or len(body) > _MAX_CA_FILE_SIZE
            or _PEM_CERTIFICATE_MARKER not in body):
        raise RuntimeError("CA証明書を安全に読み込めません")
    return bytes(body)


def _resolve_ca_file():
    """Choose a verified Python CA bundle or the fixed macOS system bundle."""
    default_path = ssl.get_default_verify_paths().cafile
    candidates = ([default_path] if default_path else []) + [_MACOS_CA_FILE]
    for candidate in dict.fromkeys(candidates):
        try:
            return _validate_ca_file(candidate)
        except RuntimeError:
            continue
    raise RuntimeError("CA証明書を安全に読み込めません")


def _build_verified_ssl_context():
    trusted_pem = _resolve_ca_file()
    try:
        trusted_text = trusted_pem.decode("ascii")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=trusted_text)
    except (AttributeError, UnicodeDecodeError, ValueError, ssl.SSLError):
        raise RuntimeError("CA証明書を安全に読み込めません") from None
    if (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
            or context.minimum_version < ssl.TLSVersion.TLSv1_2):
        raise RuntimeError("TLS証明書検証を有効にできません")
    return context


class FtpsDeployer:
    _MODE_701_PERMANENT = (
        "/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
    )
    _MODE_701_STAGING = re.compile(
        r"\A/private/[.]fixed-staging-[0-9a-f]{32}/bootstrap/"
        r"mail-forward-command-701[.]php\Z"
    )

    def __init__(self, host, username, password, *, port=21, timeout=30,
        config_remote_path="/private/config.json", filesystem_home=None,
                 ftp_factory=None):
        if filesystem_home is not None:
            if not isinstance(filesystem_home, str):
                raise ValueError("filesystem_home is invalid")
            if "." in filesystem_home.split("/"):
                raise ValueError("filesystem_home is invalid")
            pure_home = PurePosixPath(filesystem_home)
            if (len(pure_home.parts) != 3
                    or pure_home.parts[:2] != ("/", "home")
                    or pure_home.parts[2] in ("", ".", "..")
                    or pure_home.parts[2].casefold() == "public_html"):
                raise ValueError("filesystem_home is invalid")
        self.host = host
        self.username = username
        self._password = password
        self.port = port
        self.timeout = timeout
        self.config_remote_path = config_remote_path
        self.filesystem_home = filesystem_home
        self._ftp_factory = ftp_factory or (lambda: FTP_TLS(context=_build_verified_ssl_context()))

    @staticmethod
    def _validate_private(path):
        if "." in path.split("/"):
            raise ValueError("remote path must be absolute and outside public_html")
        pure = PurePosixPath(path)
        if (not pure.is_absolute() or ".." in pure.parts
                or any(part.casefold() == "public_html" for part in pure.parts)):
            raise ValueError("remote path must be absolute and outside public_html")

    def _connect(self):
        ftp = self._ftp_factory()
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login(self.username, self._password)
        ftp.set_pasv(True)
        ftp.prot_p()
        return ftp

    @classmethod
    def _validate_file_mode(cls, remote_path, mode):
        if mode not in ("600", "700", "701"):
            raise ValueError("remote mode is invalid")
        if (mode == "701" and remote_path != cls._MODE_701_PERMANENT
                and cls._MODE_701_STAGING.fullmatch(remote_path) is None):
            raise ValueError("remote mode is invalid")

    @staticmethod
    def _ensure_dirs(ftp, directory):
        current = ""
        for part in PurePosixPath(directory).parts[1:]:
            current += "/" + part
            try:
                ftp.mkd(current)
            except error_perm:
                pass
            ftp.sendcmd("SITE CHMOD 700 %s" % current)

    def _upload_atomic(self, ftp, remote_path, stream, mode="600", suffix=None):
        self._validate_private(remote_path)
        self._validate_file_mode(remote_path, mode)
        self._ensure_dirs(ftp, posixpath.dirname(remote_path))
        temporary = remote_path + (suffix or ".tmp-" + uuid.uuid4().hex)
        ftp.storbinary("STOR " + temporary, stream)
        ftp.sendcmd("SITE CHMOD %s %s" % (mode, temporary))
        ftp.rename(temporary, remote_path)

    def _log_directory(self, config):
        log_path = config.get("log_path")
        if log_path is None:
            return None
        if not isinstance(log_path, str):
            raise ValueError("log_path must be inside private root")
        self._validate_private(log_path)
        pure_log_path = PurePosixPath(log_path)
        ftps_private_root = PurePosixPath(self.config_remote_path).parent
        if self.filesystem_home is None:
            raise ValueError("filesystem_home is required for log_path")
        filesystem_private_root = PurePosixPath(self.filesystem_home).joinpath(
            *ftps_private_root.parts[1:]
        )
        try:
            relative_log_path = pure_log_path.relative_to(filesystem_private_root)
        except ValueError:
            raise ValueError("log_path must be inside private root") from None
        if not relative_log_path.parts:
            raise ValueError("log_path must be inside private root")
        return (ftps_private_root / relative_log_path.parent).as_posix()

    def deploy_release(self, local_dir, remote_root):
        self._validate_private(remote_root)
        root = Path(local_dir)
        if not root.is_dir():
            raise ValueError("local release directory does not exist")
        uploads = []
        for local_path in sorted(path for path in root.rglob("*") if path.is_file()):
            local_mode = stat.S_IMODE(os.lstat(local_path).st_mode)
            if local_mode not in (0o600, 0o700, 0o701):
                raise ValueError("local release file mode is invalid")
            relative = local_path.relative_to(root).as_posix()
            remote_path = posixpath.join(remote_root.rstrip("/"), relative)
            mode = format(local_mode, "03o")
            self._validate_file_mode(remote_path, mode)
            uploads.append((local_path, remote_path, mode))
        ftp = self._connect()
        try:
            self._ensure_dirs(ftp, remote_root)
            for local_path, remote_path, mode in uploads:
                with local_path.open("rb") as stream:
                    self._upload_atomic(ftp, remote_path, stream, mode=mode)
        finally:
            ftp.quit()

    def read_bytes(self, remote_path, *, limit):
        """Download at most ``limit`` bytes plus one, rejecting oversized data."""
        self._validate_private(remote_path)
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("readback limit is invalid")
        body = bytearray()

        def receive(chunk):
            remaining = limit + 1 - len(body)
            if remaining > 0:
                body.extend(chunk[:remaining])

        ftp = self._connect()
        try:
            ftp.retrbinary("RETR " + remote_path, receive)
        finally:
            ftp.quit()
        if len(body) > limit:
            raise RuntimeError("remote readback is too large")
        return bytes(body)

    def read_optional_bytes(self, remote_path, *, limit):
        """Return None only for an FTP 550 missing-path response."""
        try:
            return self.read_bytes(remote_path, limit=limit)
        except error_perm as error:
            if not str(error).startswith("550"):
                raise
            return None

    def assert_file_mode(self, remote_path, expected_mode):
        """Require an exact owner-only mode reported by MLST."""
        self._validate_private(remote_path)
        self._validate_file_mode(remote_path, expected_mode)
        ftp = self._connect()
        try:
            response = ftp.sendcmd("MLST " + remote_path)
        finally:
            ftp.quit()
        if not isinstance(response, str):
            raise RuntimeError("remote file mode could not be verified")
        matches = re.findall(r"(?i)(?:unix[.]mode|perm-mode)=0?([0-7]{3});", response)
        if matches != [expected_mode]:
            raise RuntimeError("remote file mode could not be verified")

    @staticmethod
    def _verify_mlst_mode(response, remote_path, expected_mode):
        """Bind one canonical absolute-path MLST entry to its requested file."""
        if not isinstance(response, str):
            raise RuntimeError("remote file mode could not be verified")
        entries = []
        for raw_line in response.splitlines():
            line = raw_line.strip()
            if line.startswith("250 "):
                line = line[4:]
            if ";" not in line or " " not in line:
                continue
            fact_text, pathname = line.split(None, 1)
            tokens = [token for token in fact_text.split(";") if token]
            if not pathname or not tokens or any("=" not in token for token in tokens):
                continue
            facts = {}
            duplicate = False
            for token in tokens:
                name, value = token.split("=", 1)
                name = name.casefold()
                if name in facts:
                    duplicate = True
                    break
                facts[name] = value
            if not duplicate and facts.get("type", "").casefold() == "file":
                entries.append((pathname, facts))
        if len(entries) != 1:
            raise RuntimeError("remote file mode could not be verified")
        pathname, facts = entries[0]
        if pathname != remote_path:
            raise RuntimeError("remote file mode could not be verified")
        mode_values = [facts[name] for name in ("unix.mode", "perm-mode") if name in facts]
        if mode_values != ["0" + expected_mode]:
            raise RuntimeError("remote file mode could not be verified")

    def verify_private_files(self, expected, *, allow_all_missing=False):
        """Verify bytes and modes for a private file set over one FTPS session.

        Return ``False`` only when all paths are missing and that state is
        explicitly allowed. Partial presence and every mismatch fail closed.
        """
        if not isinstance(expected, dict) or not expected:
            raise ValueError("remote verification set is invalid")
        checked = []
        for remote_path, value in expected.items():
            self._validate_private(remote_path)
            if (not isinstance(value, tuple) or len(value) != 2
                    or not isinstance(value[0], bytes)
                    or value[1] not in ("600", "700", "701")):
                raise ValueError("remote verification set is invalid")
            self._validate_file_mode(remote_path, value[1])
            checked.append((remote_path, value[0], value[1]))

        missing = 0
        ftp = self._connect()
        try:
            for remote_path, expected_body, expected_mode in checked:
                body = bytearray()

                def receive(chunk):
                    remaining = len(expected_body) + 1 - len(body)
                    if remaining > 0:
                        body.extend(chunk[:remaining])

                try:
                    ftp.retrbinary("RETR " + remote_path, receive)
                except error_perm as error:
                    if str(error).startswith("550"):
                        missing += 1
                        continue
                    raise RuntimeError("remote file could not be verified") from None
                if bytes(body) != expected_body:
                    raise RuntimeError("remote file content could not be verified")
                self._verify_mlst_mode(
                    ftp.sendcmd("MLST " + remote_path), remote_path, expected_mode
                )
        finally:
            ftp.quit()

        if missing == len(checked) and allow_all_missing:
            return False
        if missing:
            raise RuntimeError("remote file set could not be verified")
        return True

    def verify_private_file_hashes(self, expected):
        """Verify an exact private file set by size, SHA-256 and mode."""
        if not isinstance(expected, dict) or not expected:
            raise ValueError("remote hash verification set is invalid")
        checked = []
        for remote_path, value in expected.items():
            self._validate_private(remote_path)
            if (not isinstance(value, dict) or set(value) != {"size", "sha256", "mode"}
                    or type(value["size"]) is not int or value["size"] < 0
                    or not isinstance(value["sha256"], str)
                    or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None
                    or value["mode"] not in ("600", "700", "701")):
                raise ValueError("remote hash verification set is invalid")
            self._validate_file_mode(remote_path, value["mode"])
            checked.append((remote_path, value))
        ftp = self._connect()
        try:
            for remote_path, expected_value in checked:
                digest = hashlib.sha256()
                size = 0
                def receive(chunk):
                    nonlocal size
                    size += len(chunk)
                    if size > expected_value["size"]:
                        raise RuntimeError("remote file content could not be verified")
                    digest.update(chunk)
                try:
                    ftp.retrbinary("RETR " + remote_path, receive)
                    if (size != expected_value["size"]
                            or digest.hexdigest() != expected_value["sha256"]):
                        raise RuntimeError("remote file content could not be verified")
                    self._verify_mlst_mode(
                        ftp.sendcmd("MLST " + remote_path), remote_path,
                        expected_value["mode"])
                except (RuntimeError,) + all_errors:
                    raise RuntimeError("remote file could not be verified") from None
        finally:
            ftp.quit()
        return True

    def verify_private_file_hash_subset(self, expected, present_paths):
        """Verify every file in an authoritative exact present-path subset."""
        if not isinstance(expected, dict) or not expected:
            raise ValueError("remote hash verification set is invalid")
        checked = []
        for remote_path, value in expected.items():
            self._validate_private(remote_path)
            if (not isinstance(value, dict) or set(value) != {"size", "sha256", "mode"}
                    or type(value["size"]) is not int or value["size"] < 0
                    or not isinstance(value["sha256"], str)
                    or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None
                    or value["mode"] not in ("600", "700", "701")):
                raise ValueError("remote hash verification set is invalid")
            self._validate_file_mode(remote_path, value["mode"])
            checked.append((remote_path, value))
        if (not isinstance(present_paths, (list, tuple, set, frozenset))
                or any(type(path) is not str for path in present_paths)
                or len(present_paths) != len(set(present_paths))
                or not set(present_paths).issubset(expected)):
            raise ValueError("remote hash verification subset is invalid")
        required = set(present_paths)
        if not required:
            return frozenset()
        ftp = self._connect()
        try:
            for remote_path, expected_value in checked:
                if remote_path not in required:
                    continue
                digest = hashlib.sha256()
                size = 0

                def receive(chunk):
                    nonlocal size
                    size += len(chunk)
                    if size > expected_value["size"]:
                        raise RuntimeError("remote file content could not be verified")
                    digest.update(chunk)

                try:
                    ftp.retrbinary("RETR " + remote_path, receive)
                    if (size != expected_value["size"]
                            or digest.hexdigest() != expected_value["sha256"]):
                        raise RuntimeError("remote file content could not be verified")
                    self._verify_mlst_mode(
                        ftp.sendcmd("MLST " + remote_path), remote_path,
                        expected_value["mode"])
                except (RuntimeError,) + all_errors:
                    raise RuntimeError("remote file could not be verified") from None
        finally:
            ftp.quit()
        return frozenset(required)

    def publish_directory(self, staging_path, final_path):
        """Publish one fully validated sibling directory with one rename."""
        self._validate_private(staging_path)
        self._validate_private(final_path)
        if posixpath.dirname(staging_path) != posixpath.dirname(final_path):
            raise ValueError("publish paths must be siblings")
        ftp = self._connect()
        try:
            ftp.rename(staging_path, final_path)
        finally:
            ftp.quit()

    def delete_exact_tree(self, root, relative_files, relative_directories):
        """Delete only a previously validated allowlisted staging tree."""
        self._validate_private(root)
        ftp = self._connect()
        try:
            for relative in relative_files:
                try:
                    ftp.delete(root + "/" + relative)
                except error_perm as error:
                    if not str(error).startswith("550"):
                        raise
            for relative in sorted(relative_directories, key=lambda value: value.count("/"), reverse=True):
                try:
                    ftp.rmd(root + "/" + relative)
                except error_perm as error:
                    if not str(error).startswith("550"):
                        raise
            ftp.rmd(root)
        finally:
            ftp.quit()

    def replace_bytes_atomic(self, remote_path, body, *, mode="600"):
        """Upload, chmod and rename one private file, then return its readback."""
        self._validate_private(remote_path)
        if not isinstance(body, bytes) or mode not in ("600", "700"):
            raise ValueError("atomic replacement is invalid")
        ftp = self._connect()
        try:
            self._upload_atomic(ftp, remote_path, io.BytesIO(body), mode=mode)
        finally:
            ftp.quit()
        return self.read_bytes(remote_path, limit=len(body))

    def update_private_config(self, config):
        self._validate_private(self.config_remote_path)
        log_directory = self._log_directory(config)
        body = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ftp = self._connect()
        try:
            if log_directory is not None:
                self._ensure_dirs(ftp, log_directory)
            self._upload_atomic(ftp, self.config_remote_path, io.BytesIO(body))
        finally:
            ftp.quit()

    def read_private_config(self):
        """Read and validate the complete remote JSON object over protected FTPS."""
        self._validate_private(self.config_remote_path)
        body = io.BytesIO()
        ftp = self._connect()
        try:
            ftp.retrbinary("RETR " + self.config_remote_path, body.write)
        finally:
            ftp.quit()
        try:
            config = json.loads(body.getvalue().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("remote private config is not valid JSON") from error
        if not isinstance(config, dict):
            raise RuntimeError("remote private config must be a JSON object")
        return config
