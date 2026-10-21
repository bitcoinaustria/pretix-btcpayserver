"""Manual exceptions use pretix's approval hold, never unpaid ticket validity or forced quota."""
from datetime import timedelta

from django.utils.timezone import now
from pretix.base.models import Order, OrderPayment
from pretix.base.services.orders import OrderError, extend_order

from . import state


def active(payment):
    return bool(payment.info_data.get('review_required') and payment.order.status == Order.STATUS_PENDING
                and payment.order.require_approval)


def track(provider, payment, invoice, previous):
    """Remember observed funds, including funds subsequently removed by replacement. No webhook claims are trusted."""
    from .sync import _decimal, _note, _update_info

    if not getattr(provider, 'manual_review', False) and not payment.info_data.get('seen_at'):
        return
    seen = (invoice.get('status') == state.PROCESSING or _decimal(invoice.get('paidAmount')) > 0
            or previous.get('status') == state.PROCESSING or _decimal(previous.get('paid_amount')) > 0)
    if seen and not payment.info_data.get('seen_at'):
        _update_info(payment, seen_at=int(now().timestamp()))
    if (payment.info_data.get('seen_at') and invoice.get('status') not in (state.SETTLED, state.EXPIRED, state.INVALID)
            and payment.order.status == Order.STATUS_PENDING):
        age = now().timestamp() - payment.info_data['seen_at']
        for hours in (2, 24):
            if age >= hours * 3600:
                _note(payment, f'confirmation_delayed_{hours}h', invoice)


def hold(provider, payment, invoice):
    """True while this exception needs approval; native approval/denial is the operator's resolution action."""
    from .sync import _decimal, _note, _order_rows, _update_info

    if not (getattr(provider, 'manual_review', False) or payment.info_data.get('review_required')):
        return False
    with _order_rows(payment.order_id) as (order, payments):
        # Resolution and creation share this uninterrupted lock: native approval cannot slip between them.
        info = payments[payment.pk].info_data
        if info.get('review_required') and (
                order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED) or not order.require_approval):
            resolve(payments)
            payment.refresh_from_db()
            return False
        counted = payments[payment.pk].state in (OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_REFUNDED)
        needed = (getattr(provider, 'manual_review', False) and state.needs_manual_review(
            invoice.get('status'), seen=bool(info.get('seen_at')), paid=_decimal(invoice.get('paidAmount')) > 0))
        if counted or info.get('review_resolved') or not (needed or info.get('review_required')):
            return False
        if order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED):
            return False  # explicit cancellation is never undone
        if order.status == Order.STATUS_EXPIRED:
            try:
                extend_order(order, now() + timedelta(hours=1))  # quota check; never force an expired order back
            except OrderError:
                _note(payment, 'hold_failed', invoice)
                return False
        if not order.require_approval:
            order.require_approval = True
            order.save(update_fields=['require_approval'])
            order.create_transactions()  # Native approval holds also change pretix's accounting ledger.
            _update_info(payment, review_approval_owned=True)
        today = now().astimezone(order.event.timezone).date()
        if order.custom_followup_at is None or order.custom_followup_at > today:
            order.custom_followup_at = today
            order.save(update_fields=['custom_followup_at'])
        _update_info(payment, review_required=True)
        _note(payment, 'manual_review', invoice)
        payment.order = order
    return True


def resolve(payments):
    """Resolve all review markers under the caller's order/payment locks; retain no obsolete finite hold."""
    from .sync import PROVIDER, _update_info

    for payment in payments.values():
        if payment.provider == PROVIDER and payment.info_data.get('review_required'):
            info = payment.info_data
            info.pop('held_from', None)
            payment.info_data = info
            payment.save(update_fields=['info'])
            _update_info(payment, review_required=False, review_resolved=int(now().timestamp()))


def before_settlement(order, payments, incoming):
    """Temporarily lift our own approval flag inside the caller's atomic lock, to attempt settlement."""
    from .sync import PROVIDER, _net_paid

    owned = any(p.provider == PROVIDER and p.info_data.get('review_required')
                and p.info_data.get('review_approval_owned') for p in payments.values())
    incoming_amount = incoming.amount if incoming.state != OrderPayment.PAYMENT_STATE_CONFIRMED else 0
    if not owned or not order.require_approval or _net_paid(order) + incoming_amount < order.total:
        return False
    order.require_approval = False
    order.save(update_fields=['require_approval'])
    # Temporary within the caller's atomic order lock. Do not resolve markers before confirm() succeeds.
    return True


def after_settlement(order, payments, released):
    if not released:
        return
    order.refresh_from_db(fields=['status'])
    if order.status == Order.STATUS_PAID:
        resolve(payments)
        order.log_action('pretix_btcpay.review_settled')
    else:
        # Date policy, quota or another native restriction rejected finalization. Keep recorded money AND quota.
        order.require_approval = True
        order.save(update_fields=['require_approval'])
        order.create_transactions()
