"""Small client for XServer's documented mail-filter API."""

import json
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from manager.email_address import CanonicalEmailError, canonical_email, canonical_email_list
    from manager.ftps_deployer import _build_verified_ssl_context
except ModuleNotFoundError:
    from email_address import CanonicalEmailError, canonical_email, canonical_email_list
    from ftps_deployer import _build_verified_ssl_context


class XServerApiError(RuntimeError):
    pass


class XServerPermissionError(XServerApiError):
    pass


class XServerRateLimitError(XServerApiError):
    def __init__(self, retry_after=None):
        super().__init__("XServer API rate limit exceeded")
        self.retry_after = retry_after


def build_command_target(script_path):
    """Return Xserver's documented pipe command for one absolute PHP script."""
    parts = script_path.split("/")[1:] if isinstance(script_path, str) else []
    if (
        not isinstance(script_path, str)
        or re.fullmatch(r"/[A-Za-z0-9._/-]+", script_path) is None
        or any(part in ("", ".", "..") for part in parts)
        or any(part.casefold() == "public_html" for part in parts)
    ):
        raise ValueError("managed command path must be one absolute script path")
    return "| /usr/bin/php8.5 " + script_path


class XServerApi:
    _ORIGIN = "https://api.xserver.ne.jp"
    def __init__(self, servername, api_key, managed_command_path, *, transport=None, timeout=30):
        self.servername = servername
        self._api_key = api_key
        self.managed_command_path = managed_command_path
        self.managed_command_target = build_command_target(managed_command_path)
        self._transport = self._verified_transport if transport is None else transport
        self._timeout = timeout

    @staticmethod
    def _verified_transport(request, *, timeout):
        return urlopen(request, timeout=timeout, context=_build_verified_ssl_context())

    @property
    def _collection_url(self):
        return "%s/v1/server/%s/mail-filter" % (self._ORIGIN, quote(self.servername, safe=""))

    @property
    def _mail_collection_url(self):
        return "%s/v1/server/%s/mail" % (
            self._ORIGIN, quote(self.servername, safe="")
        )

    def _request(self, method, url, payload=None):
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Authorization": "Bearer " + self._api_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        if not request.full_url.startswith(self._ORIGIN + "/"):
            raise ValueError("XServer API requests must use the fixed HTTPS origin")
        try:
            with self._transport(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as error:
            if error.code == 403:
                raise XServerPermissionError("XServer API operation is forbidden") from None
            if error.code == 429:
                value = error.headers.get("Retry-After")
                retry_after = int(value) if value and value.isdigit() else None
                raise XServerRateLimitError(retry_after) from None
            raise XServerApiError("XServer API request failed with HTTP %d" % error.code) from None
        return json.loads(raw) if raw else {}

    def list_filters(self, domain=None):
        url = self._collection_url
        if domain is not None:
            url += "?" + urlencode({"domain": domain})
        return self._request("GET", url).get("filters", [])

    @classmethod
    def _canonical_email(cls, value):
        try:
            return canonical_email(value)
        except CanonicalEmailError:
            raise XServerApiError("XServer returned an invalid email address")

    @classmethod
    def _validate_unique_addresses(cls, values):
        try:
            addresses = canonical_email_list(values, allow_empty=True)
        except CanonicalEmailError:
            raise XServerApiError("XServer returned an invalid address list")
        if len(addresses) != len(values):
            raise XServerApiError("XServer returned duplicate email addresses")
        return addresses

    def list_mail_accounts(self, domain):
        url = self._mail_collection_url + "?" + urlencode({"domain": domain})
        accounts = self._request("GET", url).get("accounts")
        if not isinstance(accounts, list) or any(not isinstance(item, dict) for item in accounts):
            raise XServerApiError("XServer returned an invalid mail account list")
        return self._validate_unique_addresses(
            [item.get("mail_address") for item in accounts]
        )

    def list_forwarding_addresses(self, address):
        canonical = self._canonical_email(address)
        url = self._mail_collection_url + "/" + quote(canonical, safe="") + "/forwarding"
        addresses = self._request("GET", url).get("forwarding_addresses")
        return self._validate_unique_addresses(addresses)

    def discover_forwarding_sources(self, base_address):
        base = self._canonical_email(base_address)
        domain = base.rsplit("@", 1)[1]
        accounts = self.list_mail_accounts(domain)
        graph = {
            source: self.list_forwarding_addresses(source)
            for source in accounts
        }
        reachable = {base}
        changed = True
        while changed:
            additions = {
                source for source, destinations in graph.items()
                if source not in reachable and any(destination in reachable for destination in destinations)
            }
            changed = bool(additions)
            reachable.update(additions)
        return sorted(reachable)

    @staticmethod
    def canonical_filter_snapshot(filters):
        """Encode one complete API readback without discarding IDs or duplicates."""
        if not isinstance(filters, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not item["id"] for item in filters
        ):
            raise XServerApiError("XServer returned an invalid filter snapshot")
        ordered = sorted(
            filters,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        )
        return json.dumps(
            ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def snapshot_filters(self):
        """Read and canonicalize the complete filter collection (all domains)."""
        payload = self._request("GET", self._collection_url)
        filters = payload.get("filters")
        canonical = self.canonical_filter_snapshot(filters)
        return json.loads(canonical)

    def add_filter(self, rule):
        return self._request("POST", self._collection_url, rule)

    def delete_filter(self, filter_id):
        return self._request("DELETE", self._collection_url + "/" + quote(str(filter_id), safe=""))

    def is_managed_filter(self, rule):
        conditions = rule.get("conditions", [])
        action = rule.get("action", {})
        keyword = conditions[0].get("keyword") if len(conditions) == 1 else None
        try:
            canonical_keyword = self._canonical_email(keyword)
        except XServerApiError:
            canonical_keyword = None
        domain = rule.get("domain")
        keyword_domain = (canonical_keyword.rsplit("@", 1)[1]
                          if canonical_keyword is not None else None)
        return (
            len(conditions) == 1
            and conditions[0].get("field") == "header"
            and conditions[0].get("match_type") == "contain"
            and canonical_keyword is not None
            and isinstance(domain, str)
            and domain == keyword_domain
            and action.get("type") == "mail_address"
            and action.get("target") == self.managed_command_target
            and action.get("method") == "copy"
        )

    def replace_managed_filter(self, old_id, new_rule, *, old_domain=None):
        if not self.is_managed_filter(new_rule):
            raise ValueError("replacement is not a managed filter")
        old_domain = old_domain or new_rule["domain"]
        old_readback = self.list_filters(old_domain)
        old_found = any(
            item.get("id") == old_id
            and item.get("domain") == old_domain
            and self.is_managed_filter(item)
            for item in old_readback
        )
        if not old_found:
            raise RuntimeError("old managed filter was not confirmed by readback")
        new_id = self.add_filter(new_rule).get("id")
        if not new_id:
            raise RuntimeError("XServer did not return the new filter id")
        readback = self.list_filters(new_rule["domain"])
        expected = {key: new_rule[key] for key in ("domain", "conditions", "action")}
        found = any(
            item.get("id") == new_id
            and all(item.get(key) == value for key, value in expected.items())
            for item in readback
        )
        if not found:
            raise RuntimeError("new filter was not confirmed by readback")
        self.delete_filter(old_id)
        return new_id
