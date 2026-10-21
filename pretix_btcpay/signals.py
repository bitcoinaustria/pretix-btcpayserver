import logging

from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from pretix.base.signals import logentry_display, periodic_task, register_notification_types, register_payment_providers
from pretix.helpers.periodic import minimum_interval

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="pretix_btcpay")
def register_payment_provider(sender, **kwargs):
    from .payment import BTCPayServer

    return BTCPayServer


@receiver(periodic_task, dispatch_uid="pretix_btcpay_poll")
@minimum_interval(minutes_after_success=1, minutes_after_error=5)
def poll_invoices(sender, **kwargs):
    """Fallback for webhooks that never arrived: ask BTCPay about invoices that may still change."""
    from .sync import poll

    stats = poll()
    if stats["checked"]:
        logger.info("BTCPay poll: %s", stats)


LOG_TEXTS = {
    "pretix_btcpay.manual_review": _("Payment requires manual review. The order remains reserved pending approval."),
    "pretix_btcpay.review_settled": _("Confirmed funds cover the order. The payment review hold was released."),
    "pretix_btcpay.confirmation_delayed_2h": _("Payment is still unconfirmed after 2 hours."),
    "pretix_btcpay.confirmation_delayed_24h": _("Payment is still unconfirmed after 24 hours."),
    "pretix_btcpay.invoice.created": _("A BTCPay invoice was created."),
    "pretix_btcpay.processing": _("BTCPay has seen the payment; it is waiting for confirmation."),
    "pretix_btcpay.paid_partial": _("BTCPay received only part of the amount. The rest has to be returned from BTCPay."),
    "pretix_btcpay.paid_over": _("BTCPay received more than the amount. The difference has to be returned."),
    "pretix_btcpay.paid_late": _("BTCPay received the payment after the invoice expired. Check in BTCPay; marking the "
                                 "invoice as settled there confirms the payment."),
    "pretix_btcpay.marked": _("A BTCPay administrator set the invoice status by hand."),
    "pretix_btcpay.invalid_after_confirm": _("BTCPay marked an invoice as invalid after pretix had confirmed the "
                                             "payment. Please check this order by hand."),
    "pretix_btcpay.money_on_closed_invoice": _("Money arrived on a BTCPay invoice that had expired or was marked "
                                               "invalid. Check it in BTCPay: mark it as settled to confirm the "
                                               "payment, or return the money."),
    "pretix_btcpay.other_store": _("This payment's invoice was created in another BTCPay store or on another "
                                      "server than the one configured now. Check it there; pretix cannot see it any more."),
    "pretix_btcpay.double_payment": _("This order was already paid when BTCPay settled another of its invoices. The "
                                      "payment counts; the order is overpaid and the difference has to be returned."),
    "pretix_btcpay.settled_after_refund": _("BTCPay settled an invoice of a payment that was already refunded."),
    "pretix_btcpay.hold_failed": _("The order could not be kept reserved while the payment confirms."),
    "pretix_btcpay.hold_released": _("The payment was dropped; the order has its old payment deadline again."),
    "pretix_btcpay.sibling_cancelled": _("Another open BTCPay payment of this paid order was cancelled."),
    "pretix_btcpay.mismatch": _("A BTCPay invoice did not match its payment and was ignored."),
    "pretix_btcpay.refund.created": _("A BTCPay refund was created; the buyer has to claim it and the team to approve it in BTCPay."),
    "pretix_btcpay.refund.ambiguous": _("BTCPay did not answer while creating a refund. Check in BTCPay whether a pull payment with this name exists before trying again."),
    "pretix_btcpay.refund.refused": _("A refund was not sent to BTCPay, because an earlier refund of the same payment went there. Check BTCPay and do it there by hand."),
    "pretix_btcpay.order_caught_up": _("The order was marked paid after its confirmed BTCPay payment."),
    "pretix_btcpay.quota_exceeded": _("The BTCPay payment arrived, but the order could not be marked paid because its seats are gone. Add quota or return the money."),
    "pretix_btcpay.webhook.registered": _("The BTCPay webhook was registered."),
}


@receiver(logentry_display, dispatch_uid="pretix_btcpay_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    return LOG_TEXTS.get(logentry.action_type)


@receiver(register_notification_types, dispatch_uid="pretix_btcpay_review_notifications")
def review_notifications(sender, **kwargs):
    from pretix.base.notifications import Notification, NotificationType
    from pretix.helpers.urls import mainreverse_absolute

    class PaymentWarning(NotificationType):
        action_type = ''
        required_permission = "event.orders:read"
        verbose_name = _("BTCPay payment warnings")

        def __init__(self, event, action, label, title):
            super().__init__(event)
            self.action_type, self.verbose_name, self.title = action, label, title

        def build_notification(self, logentry):
            order = logentry.content_object
            url = mainreverse_absolute('control:event.order', kwargs={
                'organizer': logentry.event.organizer.slug, 'event': logentry.event.slug, 'code': order.code})
            notification = Notification(logentry.event, self.title.format(code=order.code), url=url)
            notification.add_action(_("View order"), url)
            return notification

    return [PaymentWarning(sender, 'pretix_btcpay.' + action, label, title) for action, label, title in (
        ('manual_review', _("BTCPay: manual review required"), _("Order {code}: payment requires manual review.")),
        ('confirmation_delayed_2h', _("BTCPay: unconfirmed for 2 hours"), _("Order {code}: payment unconfirmed for 2 hours.")),
        ('confirmation_delayed_24h', _("BTCPay: unconfirmed for 24 hours"), _("Order {code}: payment unconfirmed for 24 hours.")),
        ('hold_failed', _("BTCPay: reservation failed"), _("Order {code}: payment reservation could not be kept.")),
    )]


from pretix.presale.signals import order_info_top


@receiver(order_info_top, dispatch_uid="pretix_btcpay_review_notice")
def review_notice(sender, order, **kwargs):
    from django.template.loader import get_template
    from .sync import PROVIDER

    if order.status == order.STATUS_PENDING and order.require_approval:
        if any(p.info_data.get('review_required') for p in order.payments.filter(provider=PROVIDER)):
            return get_template('pretix_btcpay/review_notice.html').render({})
    return ''
