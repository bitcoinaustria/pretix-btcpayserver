"""
Checks without any dependency on pretix, Django or the network, so they can be tested on
their own (``python -m unittest discover tests``).
"""
import hashlib
import hmac
from urllib.parse import urlparse

# Permissions the API key needs for the store (btcpay.store.<name>:<storeId>), the optional one for refunds, and
# nothing else: a key that can do more (spend from the Lightning node, approve payouts, change the store) must not sit
# in pretix. Refunds only as pull payments that someone approves in BTCPay: the refund endpoint accepts any amount, and
# with btcpay.store.cancreatepullpayments it would approve some claims on its own.
REQUIRED_PERMISSIONS = (
    "btcpay.store.cancreateinvoice",
    "btcpay.store.canviewinvoices",
    "btcpay.store.webhooks.canmodifywebhooks",
)
OPTIONAL_PERMISSIONS = {
    "btcpay.store.cancreatenonapprovedpullpayments": "refunds, approved in BTCPay",
}
ALLOWED_PERMISSIONS = set(REQUIRED_PERMISSIONS) | set(OPTIONAL_PERMISSIONS)


def verify_signature(secret: str, raw_body: bytes, sig_header: str | None) -> bool:
    """BTCPay signs the raw body with HMAC-SHA256 and sends ``BTCPay-Sig: sha256=<hex>``."""
    if not secret or not isinstance(sig_header, str) or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), sig_header.strip().lower().encode())


def _effective_port(parsed) -> int:
    """The port a URL effectively uses: the explicit one, or the scheme's default."""
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def is_own_link(base_url: str, link: str) -> bool:
    """
    Whether ``link`` points at the configured BTCPay instance, so we never redirect a buyer elsewhere.
    The scheme has to match exactly; the host is compared case-insensitively (``urlparse`` already
    lowercases it) and a default port counts as its absence, so ``https://btcpay.example.com:443`` is
    the same instance as ``https://btcpay.example.com``.
    """
    try:
        # Browsers read a backslash as a slash and drop whitespace and control characters, Python does not:
        # ``https://evil.example\@pay.example.org/`` is pay.example.org to urlparse and evil.example in a browser.
        # Our instance's links never contain any of these, nor credentials, so refuse instead of normalizing.
        if not isinstance(link, str) or "\\" in link or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in link):
            return False
        base, target = urlparse(base_url), urlparse(link)
        if "@" in target.netloc:
            return False
        if base.scheme not in ("https", "http") or target.scheme != base.scheme:
            return False
        if not base.hostname or not target.hostname:
            return False
        return (target.hostname, _effective_port(target)) == (base.hostname, _effective_port(base))
    except ValueError:
        return False


def missing_permissions(permissions: list[str], store_id: str) -> tuple[list[str], list[str], list[str]]:
    """
    Check the permissions of an API key against the allowlist, all scoped to exactly this store. Returns missing
    required ones, missing optional ones, and every permission the key has beyond that (other stores included).
    """
    granted, extra = set(), []
    for perm in permissions or []:
        name, _, scope = str(perm).partition(":")
        if name in ALLOWED_PERMISSIONS and scope == store_id:
            granted.add(name)
        else:
            extra.append(str(perm))
    required = [p for p in REQUIRED_PERMISSIONS if p not in granted]
    optional = [p for p in OPTIONAL_PERMISSIONS if p not in granted]
    return required, optional, extra
