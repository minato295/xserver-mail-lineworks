"""Canonical RFC 5322 dot-atom mailbox validation shared by manager clients."""

from __future__ import annotations

import re


_LOCAL_PART = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z"
)
_DOMAIN_LABEL = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class CanonicalEmailError(ValueError):
    """Raised when an email value cannot be represented canonically."""


def canonical_email(value: object) -> str:
    """Validate one mailbox, preserving the local part and folding only its domain."""
    if type(value) is not str or value.count("@") != 1:
        raise CanonicalEmailError("invalid email address")
    local, domain = value.rsplit("@", 1)
    labels = domain.split(".")
    if (
        len(local) > 64
        or _LOCAL_PART.fullmatch(local) is None
        or len(domain) > 253
        or len(labels) < 2
        or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels)
        or len(value) > 254
    ):
        raise CanonicalEmailError("invalid email address")
    return local + "@" + domain.lower()


def canonical_email_list(
    value: object, *, allow_empty: bool, reject_duplicates: bool = False
) -> list[str]:
    """Validate, canonicalize, byte-sort, and deduplicate a list of mailboxes."""
    if type(value) is not list:
        raise CanonicalEmailError("invalid email address list")
    canonical = [canonical_email(item) for item in value]
    normalized = sorted(set(canonical), key=lambda item: item.encode("ascii"))
    if not allow_empty and not normalized:
        raise CanonicalEmailError("empty email address list")
    if reject_duplicates and value != normalized:
        raise CanonicalEmailError("email address list is not canonical")
    return normalized
