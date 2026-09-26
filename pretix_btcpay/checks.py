"""
Checks without any dependency on pretix, Django or the network, so they can be tested on
their own (``python -m unittest discover tests``).
"""
import hashlib
import hmac
from urllib.parse import urlparse

# Permissions the API key needs for the store (btcpay.store.<name>:<storeId>), and the ones
# that enable optional features.
REQUIRED_PERMISSIONS = (
    "btcpay.store.cancreateinvoice",
    "btcpay.store.canviewinvoices",
    "btcpay.store.webhooks.canmodifywebhooks",
)
OPTIONAL_PERMISSIONS = {
    "btcpay.store.canmodifyinvoices": "invalidate unpaid invoices of cancelled payments",
    "btcpay.store.cancreatepullpayments": "refunds",
}
# Permissions a key for this plugin should not have: it would let a leaked key move funds or
# change the store.
TOO_POWERFUL = (
    "unrestricted",
    "btcpay.server.canmodifyserversettings",
    "btcpay.store.canmodifystoresettings",
    "btcpay.store.canmanagepayouts",
    "btcpay.store.canmodifypaymentrequests",
)


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
    Split the permissions of an API key (``btcpay.store.x:<storeId>`` or ``btcpay.store.x`` for all
    stores) into missing required ones, missing optional ones and ones that are too powerful.
    """
    granted = set()
    for perm in permissions or []:
        name, _, scope = str(perm).partition(":")
        if not scope or scope == store_id:
            granted.add(name)
    # Only the parents that certainly include everything; anything finer is checked by name.
    implied = {
        "btcpay.store.canmodifystoresettings": set(REQUIRED_PERMISSIONS) | set(OPTIONAL_PERMISSIONS),
        "unrestricted": set(REQUIRED_PERMISSIONS) | set(OPTIONAL_PERMISSIONS),
    }
    effective = set(granted)
    for perm in granted:
        effective |= implied.get(perm, set())
    required = [p for p in REQUIRED_PERMISSIONS if p not in effective]
    optional = [p for p in OPTIONAL_PERMISSIONS if p not in effective]
    powerful = [p for p in TOO_POWERFUL if p in granted]
    return required, optional, powerful
