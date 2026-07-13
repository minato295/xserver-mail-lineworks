#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if command -v composer >/dev/null 2>&1; then
    composer validate --strict --no-check-publish
else
    php -r '
        foreach (["composer.json", "composer.lock"] as $file) {
            $data = json_decode(file_get_contents($file), true, 512, JSON_THROW_ON_ERROR);
            if (!is_array($data)) { throw new RuntimeException("Invalid " . $file); }
        }
        $manifest = json_decode(file_get_contents("composer.json"), true, 512, JSON_THROW_ON_ERROR);
        $lock = json_decode(file_get_contents("composer.lock"), true, 512, JSON_THROW_ON_ERROR);
        $names = array_column($lock["packages"] ?? [], "name");
        if (($manifest["require"]["php"] ?? null) !== ">=8.1"
            || ($manifest["require"]["zbateson/mail-mime-parser"] ?? null) !== "^4.0"
            || !in_array("zbateson/mail-mime-parser", $names, true)) {
            throw new RuntimeException("Composer manifest and lock are inconsistent");
        }
        echo "PASS: Composer manifest and lock metadata\n";
    '
fi

php_files=()
while IFS= read -r file; do
    php_files+=("$file")
done < <(git ls-files '*.php')
for file in "${php_files[@]}"; do
    php -l "$file" >/dev/null
done

php -d assert.exception=1 tests/php/test_mail_parser.php
php -d assert.exception=1 tests/php/test_stdin_frame.php
php -d assert.exception=1 tests/php/test_system_mail.php
php -d assert.exception=1 tests/php/test_sendmail_client.php
php -d assert.exception=1 tests/php/test_health_monitor.php
php -d assert.exception=1 tests/php/test_delivery.php
php -d assert.exception=1 tests/php/test_stable_bootstrap.php
php -d assert.exception=1 tests/php/test_release_validator.php
php -d assert.exception=1 tests/php/test_validate_release_entrypoint.php
php -d assert.exception=1 tests/php/test_manage_private_config.php
python3 -m unittest discover -s tests/python -v
python3 -m compileall -q macos manager tests/python

python3 - <<'PY'
from pathlib import Path
import re
import subprocess
import sys

root = Path.cwd()
tracked = subprocess.run(
    ["git", "ls-files", "-z"], check=True, capture_output=True,
).stdout.decode().split("\0")
files = [path for path in tracked if path and not path.startswith(".superpowers/")]

violations = []
email = re.compile(r"(?<![\w.+:/-])([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}))(?![\w.-])")
webhook = re.compile(r"https://webhook[.]worksmobile[.]com/message/([^\s\"'<>]+)")
known_environment = re.compile(r"(?i)\b(?:sv\d{3,}|xs\d{3,})\b|\b[a-z0-9-]+[.]xserver[.]jp\b")
public_deploy = re.compile(r"/(?:home|virtual)/(?!(?:example)(?:/|$))[^\s\"']*/public_html(?:/|\b)", re.I)
system_mail_key_value = re.compile(
    r"[\"']system_mail_hmac_key[\"']\s*(?::|=>)\s*[\"']([A-Za-z0-9_-]{43})[\"']"
)

for relative in files:
    path = root / relative
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for match in webhook.finditer(text):
        token = match.group(1).rstrip("),.;")
        if len(token) >= 20 and token not in {"REPLACE_ME", "test-placeholder", "secret-placeholder"}:
            violations.append(f"{relative}: Webhook URLらしき値")
    composer_public_emails = set()
    if relative == "composer.lock":
        try:
            lock = __import__("json").loads(text)
            for package_group in ("packages", "packages-dev"):
                for package in lock.get(package_group, []):
                    for author in package.get("authors", []):
                        if isinstance(author, dict) and isinstance(author.get("email"), str):
                            composer_public_emails.add(author["email"])
        except (TypeError, ValueError):
            violations.append(f"{relative}: Composer lock metadataが不正")
    if not relative.startswith("tests/fixtures/"):
        for match in email.finditer(text):
            domain = match.group(2).lower()
            labels = domain.split(".")
            valid_domain = all(label and not label.startswith("-") and not label.endswith("-") for label in labels)
            if valid_domain and domain != "example.invalid" and match.group(1) not in composer_public_emails:
                violations.append(f"{relative}: example.invalid以外のメールアドレス {match.group(1)}")
    sample_domain = re.compile(
        r"https?://[A-Za-z0-9.-]+[.](?:example)(?![.]invalid)(?:[:/]|\b)"
        r"|[\"']domain[\"']\s*:\s*[\"'][^\"']*[.](?:example)(?![.]invalid)(?:\b|[.:/])",
        re.I,
    )
    if sample_domain.search(text):
        violations.append(f"{relative}: サンプルドメインはexample.invalid配下に限定")
    if known_environment.search(text):
        violations.append(f"{relative}: 既知環境識別子らしき値")
    if public_deploy.search(text):
        violations.append(f"{relative}: public_html配備パスらしき値")
    if not relative.startswith("tests/") and system_mail_key_value.search(text):
        violations.append(f"{relative}: system_mail_hmac_keyの値らしき値")

secret_paths = re.compile(r"(^|/)(?:config[.]json|[.]env(?:[.]|$)|[^/]*secret[^/]*[.](?:json|ya?ml|ini|env))$", re.I)
for relative in tracked:
    if secret_paths.search(relative) and relative != "config/config.example.json":
        violations.append(f"{relative}: 秘密設定ファイルがGit追跡対象")

if violations:
    print("FAIL: public secret scan", file=sys.stderr)
    for violation in sorted(set(violations)):
        print(" - " + violation, file=sys.stderr)
    raise SystemExit(1)
print("PASS: public secret scan")
PY

echo "PASS: all tests, syntax checks, and public secret scan"
