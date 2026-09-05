"""Shared publication checks; never print matched private values."""

import re

_ACCOUNT_PATH = re.compile(r"/(?:home|virtual|Users)/([A-Za-z0-9_.-]+)(?=/)")


def contains_private_environment_path(text: str, *, test_fixture: bool = False) -> bool:
    allowed = {"example"}
    if test_fixture:
        # Existing deliberately invalid path fixtures, not real account names.
        allowed |= {".", "..", "account", "user", "a", "PUBLIC_HTML"}
    return any(match.group(1) not in allowed for match in _ACCOUNT_PATH.finditer(text))
