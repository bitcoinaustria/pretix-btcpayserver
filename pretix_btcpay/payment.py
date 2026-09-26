import logging
import secrets
from collections import OrderedDict
from decimal import Decimal
from urllib.parse import urlparse

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.loader import get_template
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString

from pretix.base.forms import SECRET_REDACTED, SecretKeySettingsField
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import eventreverse, eventreverse_absolute

from . import state
from .api import BTCPayAPI, BTCPayError, WEBHOOK_EVENTS, is_own_link, missing_permissions
from .sync import PROVIDER, event_ref, invoice_ids, sync_payment

logger = logging.getLogger(__name__)

LANGUAGES = {"de": "de-DE", "en": "en", "fr": "fr-FR", "it": "it-IT", "es": "es", "nl": "nl-NL"}


class BTCPayServer(BasePaymentProvider):
    identifier = PROVIDER
    verbose_name = _("BTCPay Server")
    execute_payment_needs_user = True
    # Pending means BTCPay has seen the transaction; giving up then invites paying twice.
    abort_pending_allowed = False

    @property
    def public_name(self):
        return str(self.settings.get("public_name", as_type=LazyI18nString) or _("Bitcoin / Lightning Network"))

    @property
    def configured(self) -> bool:
        return bool(self.settings.url and self.settings.api_key and self.settings.store_id)

    @property
    def test_mode_message(self):
        if not self.configured:
            return _("You have not yet configured your BTCPay Server URL, API key or store ID.")
        return None

    @cached_property
    def client(self) -> BTCPayAPI:
        return BTCPayAPI(url=str(self.settings.url), api_key=str(self.settings.api_key))

    @property
    def webhook_url(self) -> str:
        return eventreverse_absolute(self.event, "plugins:pretix_btcpay:webhook")

    # Setup

    def ensure_webhook(self, client: BTCPayAPI | None = None, store_id: str | None = None) -> str:
        """
        Make sure the store has exactly our webhook with a secret we know. The secret is ours
        (BTCPay accepts it on create and update), so an existing webhook is updated instead of
        deleted and recreated. Serialized on the event row, so a rush of first buyers cannot
        race each other into different secrets.
        """
        client = client or self.client
        store_id = str(store_id or self.settings.store_id)
        with transaction.atomic():
            Event.objects.select_for_update().filter(pk=self.event.pk).first()
            # Another request may have registered it while we waited for the lock.
            self.event.settings.flush()
            webhook_id, secret = self.settings.webhook_id, self.settings.webhook_secret
            found = None
            if webhook_id:
                try:
                    found = client.get_webhook(store_id, str(webhook_id))
                except BTCPayError as e:
                    if e.status != 404:
                        raise
            if found is None:
                found = next((w for w in client.list_webhooks(store_id) if w.get("url") == self.webhook_url), None)
                secret = None
            events = sorted(WEBHOOK_EVENTS)
            current = sorted((found or {}).get("authorizedEvents", {}).get("specificEvents") or [])
            if found and secret and found.get("url") == self.webhook_url and found.get("enabled") and current == events:
                return found["id"]
            secret = secret or secrets.token_urlsafe(32)
            if found:
                result = client.update_webhook(store_id, found["id"], url=self.webhook_url, secret=secret, events=WEBHOOK_EVENTS)
            else:
                result = client.create_webhook(store_id, url=self.webhook_url, secret=secret, events=WEBHOOK_EVENTS)
            self.settings.set("webhook_id", result["id"])
            self.settings.set("webhook_secret", secret)
            self.event.log_action("pretix_btcpay.webhook.registered", data={"webhook_id": result["id"], "url": self.webhook_url})
            return result["id"]

    @property
    def settings_form_fields(self):
        d = OrderedDict([
            ("url", forms.URLField(
                label=_("BTCPay Server URL"),
                help_text=_("The base URL of your BTCPay Server instance, e.g. https://btcpay.example.com"),
            )),
            ("api_key", SecretKeySettingsField(
                label=_("BTCPay Server API key"),
                help_text=_(
                    "A Greenfield API key limited to the receiving store. Required: "
                    "btcpay.store.cancreateinvoice, btcpay.store.canviewinvoices and "
                    "btcpay.store.webhooks.canmodifywebhooks. Optional: btcpay.store.canmodifyinvoices "
                    "(invalidate unpaid invoices of cancelled payments) and "
                    "btcpay.store.cancreatepullpayments (refunds). Nothing else."
                ),
            )),
            ("store_id", forms.CharField(
                label=_("Store ID"),
                help_text=_("The ID of the BTCPay Server store that receives the payments."),
            )),
            ("speed_policy", forms.ChoiceField(
                label=_("Confirmations before an on-chain payment counts"),
                choices=(
                    ("MediumSpeed", _("1 confirmation (recommended)")),
                    ("LowMediumSpeed", _("2 confirmations")),
                    ("LowSpeed", _("6 confirmations")),
                ),
                initial="MediumSpeed",
                help_text=_("Lightning payments count at once. Zero confirmations are not offered: an "
                            "unconfirmed transaction can still be replaced."),
            )),
            ("invoice_expiration", forms.IntegerField(
                label=_("Invoice expiration (minutes)"),
                help_text=_("An invoice never runs longer than the payment deadline of its order, and not longer "
                            "than this. The exchange rate is fixed for as long as the invoice runs."),
                min_value=1, max_value=1440, initial=15, required=False,
            )),
            ("public_name", I18nFormField(
                label=_("Payment method name"),
                widget=I18nTextInput,
                help_text=_("The name of the payment method that is shown to your customers during checkout."),
                required=False,
            )),
        ] + list(super().settings_form_fields.items()))
        d.move_to_end("public_name", last=False)
        d.move_to_end("_enabled", last=False)
        return d

    def settings_form_clean(self, cleaned_data):
        """Check URL, key and store before saving, and register the webhook."""
        url = cleaned_data.get(f"payment_{self.identifier}_url")
        key = cleaned_data.get(f"payment_{self.identifier}_api_key")
        if not key or key == SECRET_REDACTED:
            key = self.settings.api_key
        store_id = cleaned_data.get(f"payment_{self.identifier}_store_id")
        if not (url and key and store_id):
            return cleaned_data
        parsed = urlparse(url)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1") or (parsed.hostname or "").endswith(".localhost")
        if parsed.scheme != "https" and not local:
            raise ValidationError(_("The BTCPay Server URL has to use https."))
        client = BTCPayAPI(url=url, api_key=str(key))
        try:
            info = client.current_key()
        except BTCPayError:
            raise ValidationError(_("BTCPay Server did not accept this API key at this URL."))
        required, optional, powerful = missing_permissions(info.get("permissions") or [], str(store_id))
        if required:
            raise ValidationError(_("The API key lacks these permissions for the store: {perms}").format(perms=", ".join(required)))
        if powerful:
            raise ValidationError(_("The API key has more rights than it needs ({perms}). Please create a key with only "
                                    "the permissions listed below.").format(perms=", ".join(powerful)))
        if optional:
            logger.info("BTCPay: key for %s lacks optional permissions %s", event_ref(self.event), optional)
        # Register the webhook now, not at the first sale; this also proves store and key fit together.
        if (self.settings.url, self.settings.store_id) != (url, store_id):
            del self.settings["webhook_id"]
            del self.settings["webhook_secret"]
        try:
            self.ensure_webhook(client, str(store_id))
        except BTCPayError:
            raise ValidationError(_("The store ID is unknown to BTCPay Server, or the key is not allowed to manage its webhooks."))
        return cleaned_data

    def settings_content_render(self, request) -> str:
        template = get_template("pretix_btcpay/settings.html")
        return template.render({
            "webhook_url": self.webhook_url,
            "webhook_id": self.settings.webhook_id,
            "configured": self.configured,
            "events": WEBHOOK_EVENTS,
        })

    # Checkout

    def payment_form_render(self, request, total=None) -> str:
        return get_template("pretix_btcpay/checkout_payment_form.html").render({"request": request, "event": self.event})

    def checkout_confirm_render(self, request, order=None, info_data=None) -> str:
        return get_template("pretix_btcpay/checkout_payment_confirm.html").render({"request": request, "event": self.event})

    def payment_is_valid_session(self, request) -> bool:
        return True

    def checkout_prepare(self, request, cart) -> bool:
        return True

    def _order_url(self, order) -> str:
        return eventreverse_absolute(self.event, "presale:event.order", kwargs={"order": order.code, "secret": order.secret})

    def _pay_url(self, payment) -> str:
        # pretix' own page to continue a payment; it calls execute_payment (and only that creates invoices).
        return eventreverse_absolute(self.event, "presale:event.order.pay.complete", kwargs={
            "order": payment.order.code, "secret": payment.order.secret, "payment": payment.pk})

    def _create_invoice(self, payment: OrderPayment) -> dict:
        order = payment.order
        cap = self.settings.get("invoice_expiration", as_type=int) or self.settings.get("expiry", as_type=int) or 15
        speed = self.settings.get("speed_policy") or "MediumSpeed"
        if speed not in state.SPEED_POLICIES:
            speed = "MediumSpeed"
        checkout = {
            "speedPolicy": speed,
            "expirationMinutes": state.invoice_minutes(order.expires, now(), cap),
            # Underpaid is unpaid: a store tolerance must not let a partial payment count in full.
            "paymentTolerance": 0,
            "redirectURL": self._order_url(order),
            "redirectAutomatically": True,
        }
        language = LANGUAGES.get((order.locale or "").split("-")[0])
        if language:
            checkout["defaultLanguage"] = language
        invoice = self.client.create_invoice(
            str(self.settings.store_id),
            amount=str(payment.amount),
            currency=str(self.event.currency),
            metadata={
                "orderId": order.code,
                "pretixPaymentId": payment.pk,
                "pretixEvent": event_ref(self.event),
                # BTCPay shows these in its invoice list; no personal data.
                "itemDesc": f"{self.event.slug} {order.code}",
            },
            checkout=checkout,
        )
        if not invoice.get("id") or not is_own_link(self.client.url, invoice.get("checkoutLink") or ""):
            raise BTCPayError("Unexpected invoice from payment provider.")
        with transaction.atomic():
            locked = OrderPayment.objects.select_for_update().get(pk=payment.pk)
            info = locked.info_data
            previous = [i for i in invoice_ids(locked)]
            info.update({"invoice_id": invoice["id"], "checkout_link": invoice["checkoutLink"],
                         "previous_invoices": previous, "status": invoice.get("status"),
                         "expires": invoice.get("expirationTime")})
            for key in ("additional_status", "paid_amount", "monitoring_until", "checked"):
                info.pop(key, None)
            locked.info_data = info
            locked.save(update_fields=["info"])
        payment.refresh_from_db()
        order.log_action("pretix_btcpay.invoice.created", data={"local_id": payment.local_id, "invoice_id": invoice["id"]})
        return invoice

    def execute_payment(self, request, payment: OrderPayment):
        """
        Send the buyer to a BTCPay invoice for this payment: the current one while it can still be
        paid, otherwise a new one. A replaced invoice stays attached, so a late payment is not lost.
        """
        try:
            if not self.settings.webhook_id or not self.settings.webhook_secret:
                self.ensure_webhook()
            if payment.info_data.get("invoice_id"):
                sync_payment(self, payment, "checkout")
                payment.refresh_from_db()
                if payment.state in (OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_PENDING):
                    return self._order_url(payment.order)
                if payment.state != OrderPayment.PAYMENT_STATE_CREATED:
                    raise PaymentException(_("This payment has expired. Please start the payment again."))
                info = payment.info_data
                if info.get("status") == state.NEW and (info.get("expires") or 0) > now().timestamp() + 60:
                    return info["checkout_link"]
            return self._create_invoice(payment)["checkoutLink"]
        except BTCPayError:
            logger.exception("BTCPay: could not prepare the invoice for %s", payment.full_id)
            raise PaymentException(_("We had trouble creating your payment. Please try again and get in touch with us "
                                     "if this problem persists."))

    def payment_pending_render(self, request, payment: OrderPayment) -> str:
        info = payment.info_data
        return get_template("pretix_btcpay/pending.html").render({
            "request": request,
            "event": self.event,
            "payment": payment,
            "seen": payment.state == OrderPayment.PAYMENT_STATE_PENDING,
            "pay_url": self._pay_url(payment) if payment.state == OrderPayment.PAYMENT_STATE_CREATED else None,
            "has_invoice": bool(info.get("invoice_id")),
            "status_url": eventreverse(self.event, "plugins:pretix_btcpay:status", kwargs={
                "order": payment.order.code, "secret": payment.order.secret, "payment": payment.pk}),
        })

    def order_pending_mail_render(self, order, payment: OrderPayment) -> str:
        # Mails are rendered before execute_payment runs; link to pretix, which creates the invoice on demand.
        return str(_("To pay for your order, please visit the following page: {url}")).format(url=self._pay_url(payment))

    def cancel_payment(self, payment: OrderPayment):
        """
        pretix cancels an unpaid payment when the buyer switches the payment method. Refuse if money
        is on its way. Otherwise cancel here first, then invalidate the invoice in BTCPay (needs
        canmodifyinvoices), so nobody pays an invoice pretix no longer expects. Should money still
        arrive on it, the poll and the webhook confirm the cancelled payment (see state.decide).
        """
        invoice_id = payment.info_data.get("invoice_id")
        unpaid = False
        if invoice_id and self.configured:
            try:
                sync_payment(self, payment, "cancel")
            except BTCPayError:
                logger.warning("BTCPay: could not check invoice %s before cancelling", invoice_id)
            payment.refresh_from_db()
            info = payment.info_data
            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                raise PaymentException(_("This payment has already been received."))
            if payment.state == OrderPayment.PAYMENT_STATE_PENDING or info.get("status") == state.PROCESSING:
                raise PaymentException(_("This payment is already being processed and can not be canceled any more."))
            if info.get("status") == state.NEW and Decimal(str(info.get("paid_amount") or "0")) > 0:
                raise PaymentException(_("Part of this payment has already arrived. Please get in touch with us."))
            unpaid = info.get("status") == state.NEW
        if payment.state not in (OrderPayment.PAYMENT_STATE_FAILED, OrderPayment.PAYMENT_STATE_CANCELED):
            super().cancel_payment(payment)
        if unpaid:
            try:
                self.client.mark_invoice(str(self.settings.store_id), invoice_id, "Invalid")
            except BTCPayError as e:
                # Without the optional permission the invoice runs out on its own.
                logger.info("BTCPay: could not invalidate invoice %s (%s)", invoice_id, e.status)

    # Backend

    def payment_control_render(self, request, payment: OrderPayment) -> str:
        info = payment.info_data
        invoice, error = None, None
        if info.get("invoice_id") and self.configured:
            try:
                invoice = self.client.get_invoice(str(self.settings.store_id), info["invoice_id"])
            except BTCPayError:
                error = True
        return get_template("pretix_btcpay/control.html").render({
            "request": request,
            "payment": payment,
            "info": info,
            "invoice": invoice,
            "error": error,
            "invoice_link": f"{self.client.url}/invoices/{info['invoice_id']}" if info.get("invoice_id") else None,
            "notes": [n.split(":", 1)[1] for n in info.get("noted") or []],
        })

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        invoice_id = payment.info_data.get("invoice_id")
        return f"BTCPay {invoice_id}" if invoice_id else str(self.verbose_name)

    def payment_presale_render(self, payment: OrderPayment) -> str:
        return str(self.public_name)

    def matching_id(self, payment: OrderPayment):
        return payment.info_data.get("invoice_id")

    def api_payment_details(self, payment: OrderPayment) -> dict:
        info = payment.info_data
        return {"invoice_id": info.get("invoice_id"), "previous_invoices": info.get("previous_invoices") or [],
                "status": info.get("status"), "additional_status": info.get("additional_status")}

    # Refunds: a BTCPay pull payment the buyer claims with their own address (only for money we
    # have to return, such as double or over payments).

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return bool(payment.info_data.get("invoice_id")) and payment.info_data.get("status") == state.SETTLED

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return self.payment_refund_supported(payment)

    def execute_refund(self, refund: OrderRefund):
        payment = refund.payment
        invoice_id = payment.info_data.get("invoice_id") if payment else None
        if not invoice_id:
            raise PaymentException(_("This payment has no BTCPay invoice to refund."))
        try:
            pull = self.client.refund_invoice(
                str(self.settings.store_id), invoice_id,
                amount=str(refund.amount), currency=str(self.event.currency),
                name=f"{self.event.slug} {refund.order.code}-R-{refund.local_id}",
                description=str(_("Refund for order {code}")).format(code=refund.order.code),
            )
        except BTCPayError as e:
            if e.status == 403:
                raise PaymentException(_("The API key is not allowed to create refunds (btcpay.store.cancreatepullpayments)."))
            raise PaymentException(_("BTCPay Server could not create the refund: please do it in BTCPay."))
        link = pull.get("viewLink") or ""
        if not is_own_link(self.client.url, link):
            raise PaymentException(_("Unexpected answer from BTCPay Server."))
        refund.info_data = {"pull_payment_id": pull.get("id"), "claim_link": link}
        # The buyer still has to claim it with an address; it is done once BTCPay paid it out.
        refund.state = OrderRefund.REFUND_STATE_TRANSIT
        refund.save(update_fields=["info", "state"])
        refund.order.log_action("pretix_btcpay.refund.created", data={"local_id": refund.local_id, "pull_payment_id": pull.get("id")})

    def refund_control_render(self, request, refund: OrderRefund) -> str:
        return get_template("pretix_btcpay/refund.html").render({"refund": refund, "info": refund.info_data})

    def refund_control_render_short(self, refund: OrderRefund) -> str:
        return f"BTCPay {refund.info_data.get('pull_payment_id', '')}".strip()

    def api_refund_details(self, refund: OrderRefund):
        return {"pull_payment_id": refund.info_data.get("pull_payment_id"), "claim_link": refund.info_data.get("claim_link")}

    def shred_payment_info(self, obj):
        info = obj.info_data
        for key in ("checkout_link", "claim_link"):
            if info.get(key):
                info[key] = "█"
        obj.info_data = info
        obj.save(update_fields=["info"])
