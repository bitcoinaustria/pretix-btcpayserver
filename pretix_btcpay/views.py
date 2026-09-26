import hmac
import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from pretix.base.models import Order, OrderPayment
from pretix.base.services.locking import LockTimeoutException

from .api import WEBHOOK_EVENTS, BTCPayAPI, BTCPayError
from .sync import PROVIDER, InvoiceMismatch, apply, event_ref, invoice_ids

logger = logging.getLogger(__name__)

# BTCPay's invoice webhooks are a few hundred bytes; anything much larger is not from BTCPay.
MAX_BODY = 64 * 1024


def _provider(event):
    return event.get_payment_providers().get(PROVIDER)


@csrf_exempt
@require_POST
def webhook(request, *args, **kwargs):
    """
    BTCPay tells us an invoice changed. We only take its id from the (signed) payload and read
    the invoice back from the API, so a replayed or reordered delivery cannot change anything
    the current invoice state does not say. Non-2xx answers make BTCPay deliver again.
    """
    prov = _provider(request.event)
    if not prov or not prov.configured:
        return HttpResponse("Not configured", status=503)
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY:
        return HttpResponse("Payload too large", status=413)
    raw_body = request.body
    if len(raw_body) > MAX_BODY:
        return HttpResponse("Payload too large", status=413)
    secret = str(prov.settings.get("webhook_secret") or "")
    if not secret:
        logger.error("BTCPay: webhook for %s received but no secret configured", event_ref(request.event))
        return HttpResponse("Not configured", status=503)
    if not BTCPayAPI.verify_signature(secret, raw_body, request.META.get("HTTP_BTCPAY_SIG")):
        logger.warning("BTCPay: webhook for %s with a bad signature", event_ref(request.event))
        return HttpResponseBadRequest("Signature mismatch")

    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("Invalid JSON payload")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Invalid JSON payload")
    event_type, invoice_id = payload.get("type"), payload.get("invoiceId")
    if event_type not in WEBHOOK_EVENTS or not isinstance(invoice_id, str) or not invoice_id:
        return HttpResponse(status=200)  # a test delivery, or an event we do not act on
    if payload.get("storeId") != str(prov.settings.store_id):
        return HttpResponse(status=200)

    try:
        invoice = prov.client.get_invoice(str(prov.settings.store_id), invoice_id)
    except BTCPayError as e:
        if e.status == 404:
            return HttpResponse(status=200)
        return HttpResponse("BTCPay unavailable, please retry", status=503)

    meta = invoice.get("metadata") or {}
    if meta.get("pretixEvent") != event_ref(request.event):
        # Another event on the same store: its own webhook takes care of it.
        return HttpResponse(status=200)
    try:
        order = request.event.orders.get(code=str(meta.get("orderId")))
        payment = order.payments.get(pk=int(meta.get("pretixPaymentId")), provider=PROVIDER)
    except (Order.DoesNotExist, OrderPayment.DoesNotExist, TypeError, ValueError):
        logger.warning("BTCPay: invoice %s points to no payment of %s", invoice_id, event_ref(request.event))
        return HttpResponse(status=200)
    if invoice_id not in invoice_ids(payment):
        logger.warning("BTCPay: invoice %s is not attached to payment %s", invoice_id, payment.full_id)
        return HttpResponse(status=200)

    try:
        apply(prov, payment, invoice, f"webhook:{event_type}")
    except InvoiceMismatch as e:
        logger.warning("BTCPay: invoice %s does not match payment %s: %s", invoice_id, payment.full_id, e)
        order.log_action("pretix_btcpay.mismatch", data={"invoice_id": invoice_id, "reason": str(e)})
        return HttpResponse(status=200)
    except LockTimeoutException:
        return HttpResponse("Busy, please retry", status=503)
    return HttpResponse(status=200)


@require_GET
def status(request, *args, **kwargs):
    """
    What the pending page polls. Only for whoever has the order secret, only from the database:
    polling must not turn buyers (or anyone else) into a flood of requests against BTCPay.
    """
    try:
        order = request.event.orders.get(code=kwargs.get("order"))
    except Order.DoesNotExist:
        return HttpResponseNotFound()
    if not hmac.compare_digest(order.secret.lower().encode(), str(kwargs.get("secret", "")).lower().encode()):
        return HttpResponseNotFound()
    try:
        payment = order.payments.get(pk=int(kwargs.get("payment")), provider=PROVIDER)
    except (OrderPayment.DoesNotExist, TypeError, ValueError):
        return HttpResponseNotFound()
    response = JsonResponse({"state": payment.state, "paid": order.status == Order.STATUS_PAID})
    response["Cache-Control"] = "no-store"
    return response
