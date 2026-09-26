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


def is_own_link(base_url: str, link: str) -> bool:
    """Whether ``link`` points at the configured BTCPay instance, so we never redirect a buyer elsewhere."""
    try:
        base, target = urlparse(base_url), urlparse(link)
    except ValueError:
        return False
    return (target.scheme, target.netloc) == (base.scheme, base.netloc) and target.scheme in ("https", "http")


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
