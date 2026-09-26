import logging
from typing import Any
from urllib.parse import quote

import requests

from pretix.base.payment import PaymentException

from .checks import (  # noqa: F401  (re-exported for the provider)
    ALLOWED_PERMISSIONS, OPTIONAL_PERMISSIONS, REQUIRED_PERMISSIONS, is_own_link, missing_permissions, verify_signature,
)

logger = logging.getLogger(__name__)

# Everything that can change what an invoice means for its payment. The handler never trusts
# the payload for the state: it reads the invoice back from the API (see views.webhook).
WEBHOOK_EVENTS = [
    "InvoiceReceivedPayment",
    "InvoicePaymentSettled",
    "InvoiceProcessing",
    "InvoiceSettled",
    "InvoiceExpired",
    "InvoiceInvalid",
]

TIMEOUT = (5, 15)


class BTCPayError(PaymentException):
    """A call to BTCPay failed. ``status`` is the HTTP status, if there was a response."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _segment(value: str) -> str:
    # Store, invoice and webhook ids are short alphanumerics; never let one change the path.
    return quote(str(value), safe="")


class BTCPayAPI:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = requests.request(
                method,
                self.url + path,
                headers={"Authorization": f"token {self.api_key}", "Accept": "application/json"},
                timeout=TIMEOUT,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as e:
            logger.warning("BTCPay %s %s failed: %s", method, path, type(e).__name__)
            raise BTCPayError(
                "We had trouble communicating with the payment provider. "
                "Please try again and get in touch with us if this problem persists."
            ) from e
        if response.status_code >= 300:
            # BTCPay answers errors with JSON ({"code", "message"} or a list of field errors);
            # log it for the team, but never the request with the API key.
            logger.warning("BTCPay %s %s returned %s: %s", method, path, response.status_code, response.text[:500])
            raise BTCPayError(
                "The payment provider did not accept the request. "
                "Please try again and get in touch with us if this problem persists.",
                status=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as e:
            raise BTCPayError("Unexpected response from payment provider.", status=response.status_code) from e

    def _object(self, method, path, **kwargs) -> dict[str, Any]:
        data = self._request(method, path, **kwargs)
        if not isinstance(data, dict):
            raise BTCPayError("Unexpected response from payment provider.")
        return data

    # Invoices

    def create_invoice(self, store_id: str, *, amount: str, currency: str, metadata: dict[str, Any],
                       checkout: dict[str, Any]) -> dict[str, Any]:
        return self._object("POST", f"/api/v1/stores/{_segment(store_id)}/invoices", json={
            "amount": amount,
            "currency": currency,
            "metadata": metadata,
            "checkout": checkout,
        })

    def get_invoice(self, store_id: str, invoice_id: str) -> dict[str, Any]:
        return self._object("GET", f"/api/v1/stores/{_segment(store_id)}/invoices/{_segment(invoice_id)}")

    def refund_invoice(self, store_id: str, invoice_id: str, *, amount: str, currency: str, name: str,
                       description: str, payout_method: str | None = None) -> dict[str, Any]:
        body = {"name": name, "description": description, "refundVariant": "Custom",
                "customAmount": amount, "customCurrency": currency}
        if payout_method:
            body["payoutMethodId"] = payout_method
        return self._object("POST", f"/api/v1/stores/{_segment(store_id)}/invoices/{_segment(invoice_id)}/refund",
                            json=body)

    # Webhooks

    def list_webhooks(self, store_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/v1/stores/{_segment(store_id)}/webhooks")
        if not isinstance(data, list):
            raise BTCPayError("Unexpected response from payment provider.")
        return data

    def get_webhook(self, store_id: str, webhook_id: str) -> dict[str, Any]:
        return self._object("GET", f"/api/v1/stores/{_segment(store_id)}/webhooks/{_segment(webhook_id)}")

    @staticmethod
    def _webhook_body(url: str, secret: str, events: list[str]) -> dict[str, Any]:
        return {
            "url": url,
            "enabled": True,
            "automaticRedelivery": True,
            "authorizedEvents": {"everything": False, "specificEvents": events},
            "secret": secret,
        }

    def create_webhook(self, store_id: str, *, url: str, secret: str, events: list[str]) -> dict[str, Any]:
        return self._object("POST", f"/api/v1/stores/{_segment(store_id)}/webhooks",
                            json=self._webhook_body(url, secret, events))

    def update_webhook(self, store_id: str, webhook_id: str, *, url: str, secret: str, events: list[str]) -> dict[str, Any]:
        return self._object("PUT", f"/api/v1/stores/{_segment(store_id)}/webhooks/{_segment(webhook_id)}",
                            json=self._webhook_body(url, secret, events))

    # Setup checks

    def current_key(self) -> dict[str, Any]:
        return self._object("GET", "/api/v1/api-keys/current")

    verify_signature = staticmethod(verify_signature)
