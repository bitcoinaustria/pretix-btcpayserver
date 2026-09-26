import json
import logging
import secrets
from collections import OrderedDict
from urllib.parse import urlparse

from django import forms
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.loader import get_template
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString

from pretix.base.forms import SECRET_REDACTED, SecretKeySettingsField
from pretix.base.models import OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.helpers.database import OF_SELF
from pretix.multidomain.urlreverse import eventreverse, eventreverse_absolute

from . import state
from .api import BTCPayAPI, BTCPayError, WEBHOOK_EVENTS, is_own_link, missing_permissions
from .locks import WEBHOOK, advisory_lock
from .sync import PROVIDER, OrderBusy, event_ref, is_surplus, sync_payment

logger = logging.getLogger(__name__)

LANGUAGES = {"de": "de-DE", "en": "en", "fr": "fr-FR", "it": "it-IT", "es": "es", "nl": "nl-NL"}


class WebhookBusy(Exception):
    """Someone else is registering the webhook right now."""


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

    def ensure_webhook(self, client: BTCPayAPI | None = None, store_id: str | None = None, fresh: bool = False,
                       stage: bool = False) -> str:
        """
        Make sure the store has our webhook, with a secret we know. The secret is ours (BTCPay accepts it on create and
        update), so an existing webhook is updated instead of deleted and recreated. One registration at a time per
        event, under an advisory lock (locks.py) that lasts until the settings form is committed; no row is locked
        while talking to BTCPay. ``fresh`` ignores the stored id and secret (a new URL or store) and only stores the
        new ones on success; with ``stage`` they are only noted for that URL and store, and become active once the
        settings really use them (a settings form that fails elsewhere must not break the working webhook). Raises
        WebhookBusy if another request holds the lock.
        """
        client = client or self.client
        store_id = str(store_id or self.settings.store_id)
        with advisory_lock(WEBHOOK, self.event.pk, busy=WebhookBusy):
            self.event.settings.flush()
            webhook_id = None if fresh else self.settings.webhook_id
            secret = None if fresh else self.settings.webhook_secret
            found = None
            if webhook_id:
                try:
                    found = client.get_webhook(store_id, str(webhook_id))
                except BTCPayError as e:
                    if e.status != 404:
                        raise
            if found is None:
                found = next((w for w in client.list_webhooks(store_id) if w.get("url") == self.webhook_url), None)
                secret = None  # an existing webhook whose secret we do not know gets a new one
            events = sorted(WEBHOOK_EVENTS)
            current = sorted((found or {}).get("authorizedEvents", {}).get("specificEvents") or [])
            if found and secret and found.get("url") == self.webhook_url and found.get("enabled") and current == events:
                return found["id"]
            secret = secret or secrets.token_urlsafe(32)
            if found:
                result = client.update_webhook(store_id, found["id"], url=self.webhook_url, secret=secret, events=WEBHOOK_EVENTS)
            else:
                result = client.create_webhook(store_id, url=self.webhook_url, secret=secret, events=WEBHOOK_EVENTS)
            # Both at once, and only now: a failure above leaves the working webhook alone.
            if stage:
                self.settings.set("pending_webhook", json.dumps({"id": result["id"], "secret": secret,
                                                                 "url": client.url, "store_id": store_id}))
            else:
                self.settings.set("webhook_id", result["id"])
                self.settings.set("webhook_secret", secret)
            self.event.log_action("pretix_btcpay.webhook.registered", data={"webhook_id": result["id"], "url": self.webhook_url})
            return result["id"]

    def promote_webhook(self) -> None:
        """
        Make a staged webhook the active one once URL and store in the saved settings are the ones it was made for.
        Under the registration lock, so no registration or settings save of this provider is half done, and from the
        database rather than this Event's cached settings, which may predate the last save.
        """
        if not self.settings.get("pending_webhook"):
            return
        prefix = f"payment_{self.identifier}_"
        try:
            with transaction.atomic(), advisory_lock(WEBHOOK, self.event.pk, busy=WebhookBusy):
                stored = dict(self.event._settings_objects.filter(
                    key__in=[prefix + "pending_webhook", prefix + "url", prefix + "store_id"]).values_list("key", "value"))
                try:
                    pending = json.loads(stored.get(prefix + "pending_webhook") or "null")
                except ValueError:
                    pending = None
                if not isinstance(pending, dict):
                    return
                if (pending.get("url"), pending.get("store_id")) != (str(stored.get(prefix + "url") or "").rstrip("/"),
                                                                     str(stored.get(prefix + "store_id") or "")):
                    return
                self.settings.set("webhook_id", pending["id"])
                self.settings.set("webhook_secret", pending["secret"])
                del self.settings["pending_webhook"]
        except WebhookBusy:
            return  # a registration or a settings save is running; the next call does it
        finally:
            self.event.settings.flush()

    def webhook_secrets(self) -> list[str]:
        """The secret to check webhooks with; a staged webhook for the saved URL and store is promoted first."""
        self.promote_webhook()
        return [s for s in [str(self.settings.webhook_secret or "")] if s]

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
                    "A Greenfield API key limited to the receiving store, with exactly these permissions: "
                    "btcpay.store.cancreateinvoice, btcpay.store.canviewinvoices and "
                    "btcpay.store.webhooks.canmodifywebhooks, and btcpay.store.cancreatenonapprovedpullpayments "
                    "only if refunds are created in BTCPay. Keys with more are refused."
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
            ("refunds", forms.BooleanField(
                label=_("Create refunds in BTCPay"),
                help_text=_("Off by default: refunds are then done by hand in BTCPay and recorded in pretix. On, the "
                            "first refund of a payment in pretix creates a BTCPay pull payment that the buyer claims "
                            "and someone approves in BTCPay; further ones of the same payment are done by hand. The API "
                            "key then also needs btcpay.store.cancreatenonapprovedpullpayments."),
                required=False,
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
        required, optional, extra = missing_permissions(info.get("permissions") or [], str(store_id))
        if required:
            raise ValidationError(_("The API key lacks these permissions for the store: {perms}").format(perms=", ".join(required)))
        if extra:
            raise ValidationError(_("The API key has more rights than it needs ({perms}). Please create a key with only "
                                    "the permissions listed below.").format(perms=", ".join(extra)))
        if cleaned_data.get(f"payment_{self.identifier}_refunds") and optional:
            raise ValidationError(_("Refunds in BTCPay need the permission btcpay.store.cancreatenonapprovedpullpayments."))
        if cleaned_data.get(f"payment_{self.identifier}_refunds") and not getattr(settings, "REAL_CACHE_USED", False):
            raise ValidationError(_("Refunds in BTCPay need Redis or memcached as pretix' cache."))
        # Register the webhook now, not at the first sale; this also proves store and key fit together.
        fresh = (str(self.settings.url or ""), str(self.settings.store_id or "")) != (str(url), str(store_id))
        try:
            self.ensure_webhook(client, str(store_id), fresh=fresh, stage=fresh)
        except WebhookBusy:
            raise ValidationError(_("The webhook is being registered right now. Please save again in a minute."))
        except BTCPayError:
            raise ValidationError(_("The store ID is unknown to BTCPay Server, or the key is not allowed to manage its webhooks."))
        return cleaned_data

    def settings_content_render(self, request) -> str:
        self.promote_webhook()
        return get_template("pretix_btcpay/settings.html").render({
            "webhook_url": self.webhook_url,
            "webhook_id": self.settings.webhook_id,
            "configured": self.configured,
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

    def _checkout_options(self, order) -> dict:
        cap = self.settings.get("invoice_expiration", as_type=int) or self.settings.get("expiry", as_type=int) or 15
        speed = self.settings.get("speed_policy") or "MediumSpeed"
        checkout = {
            "speedPolicy": speed if speed in state.SPEED_POLICIES else "MediumSpeed",
            "expirationMinutes": state.invoice_minutes(order.expires, now(), cap),
            # Underpaid is unpaid: a store tolerance must not let a partial payment count in full.
            "paymentTolerance": 0,
            "redirectURL": self._order_url(order),
            "redirectAutomatically": True,
        }
        language = LANGUAGES.get((order.locale or "").split("-")[0])
        if language:
            checkout["defaultLanguage"] = language
        return checkout

    def execute_payment(self, request, payment: OrderPayment):
        """
        Send the buyer to the one BTCPay invoice of this payment, creating it on the first call. Serialized per
        payment (a double click, two tabs), so a payment never gets two invoices. A payment whose invoice ran out is
        not continued: pretix then starts a new payment, with a new invoice.
        """
        self.promote_webhook()
        if not self.settings.webhook_id or not self.settings.webhook_secret:
            # Registered when the settings are saved, never here: a rush of first buyers must not race for it. Without
            # a webhook the poll still picks every payment up, only a minute later.
            logger.error("BTCPay: no webhook registered for %s; save the payment settings once", event_ref(self.event))
        try:
            if payment.info_data.get("invoice_id"):
                try:
                    sync_payment(self, payment, "checkout", wait=5)
                except OrderBusy:
                    pass  # a webhook is applying it right now; the state below is read fresh
            with transaction.atomic():
                locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
                info = locked.info_data
                if info.get("invoice_id"):
                    if locked.state in (OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_PENDING):
                        return self._order_url(payment.order)
                    if (locked.state == OrderPayment.PAYMENT_STATE_CREATED and info.get("status") == state.NEW
                            and (info.get("expires") or 0) > now().timestamp()):
                        return info["checkout_link"]
                    raise PaymentException(_("This payment has expired. Please start the payment again."))
                if locked.state != OrderPayment.PAYMENT_STATE_CREATED:
                    raise PaymentException(_("This payment can not be continued. Please start the payment again."))
                order = payment.order
                invoice = self.client.create_invoice(
                    str(self.settings.store_id),
                    amount=str(locked.amount),
                    currency=str(self.event.currency),
                    metadata={"orderId": order.code, "pretixPaymentId": locked.pk, "pretixEvent": event_ref(self.event),
                              # BTCPay shows this in its invoice list; no personal data.
                              "itemDesc": f"{self.event.slug} {order.code}"},
                    checkout=self._checkout_options(order),
                )
                if not invoice.get("id") or not is_own_link(self.client.url, invoice.get("checkoutLink") or ""):
                    # Rolled back with the transaction; an invoice BTCPay did create runs out unpaid.
                    raise BTCPayError("Unexpected invoice from payment provider.")
                info.update({"invoice_id": invoice["id"], "checkout_link": invoice["checkoutLink"],
                             "status": invoice.get("status"), "expires": invoice.get("expirationTime")})
                locked.info_data = info
                locked.save(update_fields=["info"])
            payment.refresh_from_db()
            order.log_action("pretix_btcpay.invoice.created", data={"local_id": payment.local_id, "invoice_id": invoice["id"]})
            return invoice["checkoutLink"]
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
        pretix cancels an unpaid payment when the buyer switches the payment method. Refuse if BTCPay has seen money
        for it; otherwise cancel it here, under a row lock. The invoice is left to run out rather than invalidated in
        BTCPay: invalidating could hit a payment that arrives in the same second. Money that still arrives on it is
        picked up by the webhook and the poll and confirms the cancelled payment (see state.decide).
        """
        if payment.info_data.get("invoice_id") and self.configured:
            try:
                sync_payment(self, payment, "cancel", wait=5)
            except OrderBusy:
                logger.info("BTCPay: order of %s busy while cancelling; the poll catches up", payment.full_id)
            except BTCPayError:
                logger.warning("BTCPay: could not check the invoice of %s before cancelling", payment.full_id)
        with transaction.atomic():
            locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
            info = locked.info_data
            if locked.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                raise PaymentException(_("This payment has already been received."))
            if locked.state == OrderPayment.PAYMENT_STATE_PENDING or info.get("status") == state.PROCESSING:
                raise PaymentException(_("This payment is already being processed and can not be canceled any more."))
            if info.get("status") == state.NEW and float(info.get("paid_amount") or 0) > 0:
                raise PaymentException(_("Part of this payment has already arrived. Please get in touch with us."))
            if locked.state in (OrderPayment.PAYMENT_STATE_CREATED,):
                locked.state = OrderPayment.PAYMENT_STATE_CANCELED
                locked.save(update_fields=["state"])
        payment.refresh_from_db()

    # Backend

    def payment_control_render(self, request, payment: OrderPayment) -> str:
        info = payment.info_data
        invoice, error = None, None
        if info.get("invoice_id") and self.configured and not is_surplus(payment):
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
            "notes": [n.split(":", 1)[0] for n in info.get("noted") or []],
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
        return {"invoice_id": info.get("invoice_id"), "status": info.get("status"),
                "additional_status": info.get("additional_status"), "paid_amount": info.get("paid_amount"),
                "surplus_of": info.get("surplus_of"), "surplus_recorded": info.get("surplus_recorded")}

    # Refunds: a BTCPay pull payment that the buyer claims with their own address and the team approves in BTCPay
    # (only for money we have to return, such as double or over payments).

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        # The note that stops a second claim lives in the shared cache (execute_refund); without one, no refunds here.
        return (self.settings.get("refunds", as_type=bool, default=False) and getattr(settings, "REAL_CACHE_USED", False)
                and bool(payment.info_data.get("invoice_id")) and payment.info_data.get("status") == state.SETTLED)

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return self.payment_refund_supported(payment)

    def execute_refund(self, refund: OrderRefund):
        payment = refund.payment
        invoice_id = payment.info_data.get("invoice_id") if payment else None
        if not invoice_id:
            raise PaymentException(_("This payment has no BTCPay invoice to refund."))
        if refund.info_data.get("pull_payment_id") or refund.info_data.get("in_flight"):
            raise PaymentException(_("This refund was already sent to BTCPay."))
        name = f"pretix {self.event.slug} {refund.order.code} R{refund.local_id} #{refund.pk}"
        # One refund per payment goes through BTCPay. It is noted in the shared cache before the request and the note
        # stays: pretix runs this inside its own transaction, and if that rolls back after BTCPay created the claim,
        # the retry is a new refund and must not create a second claim. cache.add is atomic, so of two at once only
        # one gets through. Any further refund of the same payment is done by hand in BTCPay, after looking there.
        intent = f"pretix_btcpay_refund_{payment.pk}"
        if not cache.add(intent, name, timeout=None):
            earlier = cache.get(intent) or "?"
            refund.order.log_action("pretix_btcpay.refund.refused", data={"local_id": refund.local_id, "earlier": earlier})
            raise PaymentException(_("A refund of this payment was already sent to BTCPay ({earlier}). Check the pull "
                                     "payments in BTCPay, and do any further refund of this payment there by hand.")
                                   .format(earlier=earlier))
        logger.warning("BTCPay: creating refund %s for invoice %s (%s %s)", name, invoice_id, refund.amount, self.event.currency)
        refund.info_data = {"name": name, "in_flight": True}
        refund.save(update_fields=["info"])

        def ambiguous():
            refund.info_data = {"ambiguous": True, "name": name}
            refund.state = OrderRefund.REFUND_STATE_TRANSIT
            refund.save(update_fields=["info", "state"])
            refund.order.log_action("pretix_btcpay.refund.ambiguous", data={"local_id": refund.local_id, "name": name})

        try:
            pull = self.client.refund_invoice(
                str(self.settings.store_id), invoice_id, amount=str(refund.amount), currency=str(self.event.currency),
                name=name, description=str(_("Refund for order {code}")).format(code=refund.order.code),
            )
        except BTCPayError as e:
            if e.status is not None and 400 <= e.status < 500 and e.status != 408:
                # BTCPay refused: nothing was created.
                cache.delete(intent)
                refund.info_data = {"name": name}
                refund.save(update_fields=["info"])
                if e.status == 403:
                    raise PaymentException(_("The API key is not allowed to create refunds "
                                             "(btcpay.store.cancreatenonapprovedpullpayments)."))
                raise PaymentException(_("BTCPay Server could not create the refund: please do it in BTCPay."))
            return ambiguous()  # no answer, a server error or an unreadable one: it may exist
        link = pull.get("viewLink") or ""
        if not pull.get("id") or not is_own_link(self.client.url, link):
            return ambiguous()
        cache.set(intent, f"{name}, {pull.get('id')}", timeout=None)
        refund.info_data = {"pull_payment_id": pull.get("id"), "claim_link": link, "name": name}
        # The buyer still has to claim it and the team to approve it in BTCPay; it is done once BTCPay paid it out.
        refund.state = OrderRefund.REFUND_STATE_TRANSIT
        refund.save(update_fields=["info", "state"])
        refund.order.log_action("pretix_btcpay.refund.created", data={"local_id": refund.local_id, "pull_payment_id": pull.get("id")})

    def refund_control_render(self, request, refund: OrderRefund) -> str:
        return get_template("pretix_btcpay/refund.html").render({"refund": refund, "info": refund.info_data})

    def refund_control_render_short(self, refund: OrderRefund) -> str:
        return f"BTCPay {refund.info_data.get('pull_payment_id', '')}".strip()

    def api_refund_details(self, refund: OrderRefund):
        return {"pull_payment_id": refund.info_data.get("pull_payment_id"), "claim_link": refund.info_data.get("claim_link"),
                "ambiguous": refund.info_data.get("ambiguous", False)}

    def shred_payment_info(self, obj):
        info = obj.info_data
        for key in ("checkout_link", "claim_link"):
            if info.get(key):
                info[key] = "█"
        obj.info_data = info
        obj.save(update_fields=["info"])

