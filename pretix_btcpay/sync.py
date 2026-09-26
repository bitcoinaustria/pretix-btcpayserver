"""
Carry out what an invoice state means for its pretix payment (see state.py for the rules).

The webhook, the periodic poll and the payment page all end up in ``sync_payment``: it reads
the invoice from the BTCPay API (never from a webhook payload), checks that it really belongs
to this payment and applies the decision. Running it twice changes nothing the second time.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.timezone import now

from pretix.base.models import Order, OrderPayment, Quota
from pretix.base.services.locking import LockTimeoutException
from pretix.base.services.orders import OrderError, extend_order
from pretix.helpers.database import OF_SELF

from . import state
from .api import BTCPayError

logger = logging.getLogger(__name__)

PROVIDER = "btcpay_greenfield"
# BTCPay watches an unconfirmed transaction for a day by default (store setting "invoice monitoring").
DEFAULT_MONITORING = timedelta(hours=24)


class InvoiceMismatch(Exception):
    """The invoice does not belong to the payment it claims to belong to."""


def event_ref(event) -> str:
    return f"{event.organizer.slug}/{event.slug}"


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def invoice_ids(payment: OrderPayment) -> list[str]:
    """The current invoice of the payment and the ones it replaced (a late payment may still arrive there)."""
    info = payment.info_data
    ids = [info.get("invoice_id")] + list(info.get("previous_invoices") or [])
    return [i for i in ids if i]


def check_invoice(payment: OrderPayment, invoice: dict, store_id: str) -> None:
    """Raise InvoiceMismatch unless the invoice is one of ours for exactly this payment and amount."""
    meta = invoice.get("metadata") or {}
    if invoice.get("id") not in invoice_ids(payment):
        raise InvoiceMismatch("invoice is not attached to this payment")
    if invoice.get("storeId") != store_id:
        raise InvoiceMismatch("invoice belongs to another store")
    if str(meta.get("pretixPaymentId")) != str(payment.pk) or meta.get("orderId") != payment.order.code:
        raise InvoiceMismatch("invoice metadata points to another payment")
    if meta.get("pretixEvent") not in (None, event_ref(payment.order.event)):
        raise InvoiceMismatch("invoice belongs to another event")
    try:
        amount = Decimal(str(invoice.get("amount")))
    except (InvalidOperation, TypeError):
        raise InvoiceMismatch("invoice has no amount")
    if amount != payment.amount or invoice.get("currency") != payment.order.event.currency:
        raise InvoiceMismatch("invoice amount or currency differs from the payment")


def _remember(payment: OrderPayment, invoice: dict) -> None:
    """Keep what the team needs to see (status, paid amount) and what polling needs (monitoring end)."""
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        info = locked.info_data
        if invoice.get("id") == info.get("invoice_id"):
            info.update({
                "status": invoice.get("status"),
                "additional_status": invoice.get("additionalStatus"),
                "paid_amount": str(invoice.get("paidAmount") or "0"),
                "monitoring_until": invoice.get("monitoringExpiration"),
                "checked": int(now().timestamp()),
            })
        else:
            late = dict(info.get("late_invoices") or {})
            late[invoice["id"]] = {"status": invoice.get("status"), "additional_status": invoice.get("additionalStatus"),
                                   "monitoring_until": invoice.get("monitoringExpiration")}
            info["late_invoices"] = late
        locked.info_data = info
        locked.save(update_fields=["info"])
    payment.refresh_from_db()


def _note(payment: OrderPayment, note: str, invoice: dict) -> None:
    """Log something for the team once per payment and invoice."""
    key = f"{invoice.get('id')}:{note}"
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        info = locked.info_data
        noted = list(info.get("noted") or [])
        if key in noted:
            return
        info["noted"] = noted + [key]
        locked.info_data = info
        locked.save(update_fields=["info"])
    payment.refresh_from_db()
    payment.order.log_action(f"pretix_btcpay.{note}", data={
        "local_id": payment.local_id,
        "invoice_id": invoice.get("id"),
        "status": invoice.get("status"),
        "additional_status": invoice.get("additionalStatus"),
        "amount": str(invoice.get("amount")),
        "paid_amount": str(invoice.get("paidAmount")),
        "currency": invoice.get("currency"),
    })


def _hold_order(payment: OrderPayment, invoice: dict) -> None:
    """Keep the order reserved while BTCPay still waits for a seen payment to confirm."""
    order = Order.objects.get(pk=payment.order_id)
    until = state.hold_until(_timestamp(invoice.get("monitoringExpiration")), now(), DEFAULT_MONITORING)
    if order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED):
        return
    if order.status == Order.STATUS_PENDING and order.expires >= until - timedelta(minutes=5):
        return
    # Remember the deadline before the first extension, to give the seats back if the payment fails.
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        info = locked.info_data
        if "held_from" not in info:
            info["held_from"] = int(order.expires.timestamp())
            locked.info_data = info
            locked.save(update_fields=["info"])
    payment.refresh_from_db()
    try:
        extend_order(order, until)
    except OrderError as e:
        # Expired and sold out in the meantime; confirm() will say so once the payment settles.
        logger.info("BTCPay: could not extend order %s: %s", order.full_code, e)
        payment.order.log_action("pretix_btcpay.hold_failed", data={"invoice_id": invoice.get("id"), "message": str(e)})


def _release_hold(payment: OrderPayment) -> None:
    """
    A seen payment was dropped (double spend, never confirmed): give the order its old payment
    deadline back, with a few minutes to pay again, instead of holding the seats for a day.
    """
    held_from = _timestamp(payment.info_data.get("held_from"))
    order = Order.objects.get(pk=payment.order_id)
    if not held_from or order.status != Order.STATUS_PENDING:
        return
    if order.payments.filter(state=OrderPayment.PAYMENT_STATE_PENDING).exclude(pk=payment.pk).exists():
        return  # another payment is on its way and needs the reservation
    back = max(held_from, now() + timedelta(minutes=10))
    if order.expires <= back:
        return
    try:
        extend_order(order, back)
        order.log_action("pretix_btcpay.hold_released", data={"local_id": payment.local_id, "expires": back.isoformat()})
    except OrderError as e:
        logger.info("BTCPay: could not shorten order %s: %s", order.full_code, e)


def _mark_pending(payment: OrderPayment) -> None:
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        if locked.state == OrderPayment.PAYMENT_STATE_CREATED:
            locked.state = OrderPayment.PAYMENT_STATE_PENDING
            locked.save(update_fields=["state"])
            payment.order.log_action("pretix_btcpay.processing", data={"local_id": payment.local_id})
    payment.refresh_from_db()


def _close_siblings(payment: OrderPayment) -> None:
    """Once an order is paid, invalidate other unpaid BTCPay invoices of it, so nobody pays twice."""
    order = Order.objects.get(pk=payment.order_id)
    if order.status != Order.STATUS_PAID:
        return
    for other in order.payments.filter(provider=PROVIDER, state=OrderPayment.PAYMENT_STATE_CREATED).exclude(pk=payment.pk):
        try:
            other.payment_provider.cancel_payment(other)
            order.log_action("pretix_btcpay.sibling_cancelled", data={"local_id": other.local_id})
        except Exception:
            logger.exception("BTCPay: could not cancel open payment %s of paid order", other.full_id)


def apply(provider, payment: OrderPayment, invoice: dict, source: str) -> str:
    """Apply the decision for ``invoice`` to ``payment``. Returns the action taken."""
    check_invoice(payment, invoice, str(provider.settings.store_id))
    _remember(payment, invoice)
    decision = state.decide(invoice.get("status"), invoice.get("additionalStatus"), payment.state)
    for note in decision.notes:
        _note(payment, note, invoice)

    if decision.action == "pending":
        _mark_pending(payment)
        _hold_order(payment, invoice)
    elif decision.action == "hold":
        _hold_order(payment, invoice)
    elif decision.action == "fail":
        payment.fail(info=payment.info_data, log_data={"source": source, "invoice_id": invoice.get("id"),
                                                      "status": invoice.get("status")})
        _release_hold(payment)
    elif decision.action == "confirm":
        try:
            payment.confirm()
        except Quota.QuotaExceededException as e:
            # The payment counts, the order could not be marked paid (seats gone after it expired).
            # pretix logged it; the team decides between more quota and a refund.
            logger.warning("BTCPay: payment %s confirmed but order not paid: %s", payment.full_id, e)
        except LockTimeoutException:
            raise
        _close_siblings(payment)
    return decision.action


def sync_payment(provider, payment: OrderPayment, source: str, invoice_id: str | None = None) -> str | None:
    """Read the invoice (the current one, or ``invoice_id`` if it belongs to the payment) and apply it."""
    ids = invoice_ids(payment)
    target = invoice_id or (ids[0] if ids else None)
    if not target or target not in ids:
        return None
    invoice = provider.client.get_invoice(str(provider.settings.store_id), target)
    return apply(provider, payment, invoice, source)


def needs_poll(payment: OrderPayment, at: datetime) -> list[str]:
    """Invoices of a payment whose state can still change and is worth asking BTCPay about."""
    info = payment.info_data
    todo = []
    current = info.get("invoice_id")
    if current and not state.is_final(info.get("status"), _timestamp(info.get("monitoring_until")), at):
        todo.append(current)
    late = info.get("late_invoices") or {}
    for old in info.get("previous_invoices") or []:
        seen = late.get(old) or {}
        if not state.is_final(seen.get("status"), _timestamp(seen.get("monitoring_until")), at):
            todo.append(old)
    return todo


def poll(limit: int = 300, days: int = 7) -> dict:
    """
    Ask BTCPay about every payment whose invoice may still change: the fallback for webhooks that
    never arrived. Payments that failed or were cancelled are included, because money can still
    arrive on their invoice.
    """
    from django_scopes import scope, scopes_disabled

    stats = {"checked": 0, "changed": 0, "errors": 0}
    at = now()
    with scopes_disabled():
        candidates = list(
            OrderPayment.objects.filter(
                provider=PROVIDER,
                created__gte=at - timedelta(days=days),
                state__in=(OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING,
                           OrderPayment.PAYMENT_STATE_FAILED, OrderPayment.PAYMENT_STATE_CANCELED),
            ).select_related("order", "order__event", "order__event__organizer").order_by("created")[:limit * 4]
        )
    for payment in candidates:
        if stats["checked"] >= limit:
            break
        todo = needs_poll(payment, at)
        if not todo:
            continue
        event = payment.order.event
        with scope(organizer=event.organizer):
            provider = event.get_payment_providers().get(PROVIDER)
            if not provider or not provider.is_enabled or not provider.configured:
                continue
            for invoice_id in todo:
                stats["checked"] += 1
                try:
                    before = payment.state
                    sync_payment(provider, payment, "poll", invoice_id)
                    payment.refresh_from_db()
                    stats["changed"] += int(payment.state != before)
                except (BTCPayError, InvoiceMismatch, LockTimeoutException) as e:
                    stats["errors"] += 1
                    logger.warning("BTCPay: poll of %s / %s failed: %s", payment.full_id, invoice_id, e)
    return stats
