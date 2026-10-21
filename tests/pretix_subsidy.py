"""Run with Django's test runner in a disposable pretix image, never a shared installation."""
from datetime import timedelta
from decimal import Decimal as D
from inspect import signature
from unittest.mock import Mock

from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils.timezone import now
from django.utils.translation import override
from django_scopes import scopes_disabled
from pretix.base.models import CartPosition, Event, InvoiceAddress, Item, Organizer, TaxRule
from pretix.base.services.invoices import generate_invoice
from pretix.base.services.orders import _apply_rounding_and_fees, _create_order, change_payment_provider

from pretix_btcpay.payment import BTCPayServer


class SubsidyIntegration(TestCase):
    def setUp(self):
        self.enterContext(scopes_disabled())
        self.organizer = Organizer.objects.create(name="Subsidy tests", slug="subsidy",
                                                  plugins="pretix.plugins.banktransfer")
        self.event = Event.objects.create(organizer=self.organizer, name="Subsidy tests", slug="test",
                                          date_from=now(), currency="EUR", testmode=True,
                                          plugins="pretix_btcpay,pretix.plugins.banktransfer")
        self.provider = BTCPayServer(self.event)
        self.provider.settings.set("subsidy_percent", "5.00")
        self.provider.settings.set("_fee_reverse_calc", False)
        self.provider.settings.set("url", "https://pay.example.org")
        self.provider.settings.set("store_id", "TEST")
        self.provider.settings.set("webhook_id", "TEST")
        self.provider.settings.set("webhook_secret", "test-only-webhook-secret")
        self.channel = self.organizer.sales_channels.get(identifier="web")
        self.item = Item.objects.create(event=self.event, name="Pass", default_price=D("100"), admission=True)

    def order(self, price="100.00"):
        tax = (self.item.tax_rule or TaxRule.zero()).tax(D(price), base_price_is="gross")
        position = CartPosition.objects.create(event=self.event, item=self.item, price=D(price),
                                                tax_rate=tax.rate, tax_value=tax.tax,
                                                cart_id="subsidy-test", expires=now() + timedelta(minutes=30))
        requests = [{"provider": self.provider.identifier, "pprov": self.provider, "info_data": {}}]
        order, payments = _create_order(self.event, email=None, positions=[position], now_dt=now(),
                                         payment_requests=requests, sales_channel=self.channel, locale="en")
        return order, payments[0]

    def test_real_provider_defaults_and_fee_composition(self):
        self.provider.settings.set("subsidy_percent", "")
        self.provider.settings.set("_fee_abs", "2.00")
        self.assertEqual(self.provider.calculate_fee(D("100")), D("2.00"))
        del self.provider.settings["subsidy_percent"]
        self.assertEqual(self.provider.calculate_fee(D("100")), D("2.00"))
        self.provider.settings.set("subsidy_percent", "5")
        self.assertEqual(self.provider.calculate_fee(D("100")), D("-3.00"))
        self.provider.settings.set("_fee_abs", "0")
        self.provider.settings.set("_fee_percent", "2.5")
        self.provider.settings.set("_fee_reverse_calc", True)
        self.assertEqual(self.provider.calculate_fee(D("100")), D("-2.44"))

    def test_settings_field_accepts_localized_percent_and_rejects_invalid_values(self):
        with override("de"):
            field = self.provider.settings_form_fields["subsidy_percent"]
            self.assertEqual(field.clean("99,50"), D("99.50"))
            for value in ("-1", "100", "NaN", "Infinity", "5,555"):
                with self.subTest(value=value), self.assertRaises(ValidationError):
                    field.clean(value)
            self.assertIsNone(field.clean(""))

    def test_checkout_notice_is_translated_and_hidden_when_disabled(self):
        with override("de"):
            html = self.provider.payment_form_render(RequestFactory().get("/"))
            self.assertIn("5% Rabatt", html)
            self.assertIn("Veranstalter", html)
            self.assertNotIn("%(percent)", html)
            self.provider.settings.set("subsidy_percent", "0")
            self.assertNotIn("Rabatt", self.provider.payment_form_render(RequestFactory().get("/")))
            self.provider.settings.set("subsidy_percent", "")
            self.assertNotIn("Rabatt", self.provider.payment_form_render(RequestFactory().get("/")))

    def test_order_invoice_and_btcpay_use_the_same_reduced_amount(self):
        order, payment = self.order()
        self.assertEqual(order.total, D("95.00"))
        self.assertEqual(payment.amount, D("95.00"))
        self.assertEqual(payment.fee.value, D("-5.00"))
        invoice = generate_invoice(order)
        self.assertEqual(sum(line.gross_value for line in invoice.lines.all()), D("95.00"))
        client = Mock(url="https://pay.example.org")
        client.create_invoice.return_value = {"id": "TEST", "checkoutLink": "https://pay.example.org/i/TEST",
                                               "status": "New", "expirationTime": (now() + timedelta(minutes=10)).timestamp()}
        self.provider.__dict__["client"] = client
        self.provider.execute_payment(RequestFactory().get("/"), payment)
        self.assertEqual(client.create_invoice.call_args.kwargs["amount"], "95.00")
        # Later settings changes do not rewrite an invoice that already exists.
        self.provider.settings.set("subsidy_percent", "10")
        payment.refresh_from_db()
        self.assertEqual(payment.amount, D("95.00"))
        self.assertEqual(order.total, D("95.00"))

    def test_payment_method_switch_removes_and_reapplies_subsidy_without_stacking(self):
        order, payment = self.order()
        bank = self.event.get_payment_providers()["banktransfer"]
        _, _, _, other, _ = change_payment_provider(order, bank, recreate_invoices=False)
        order.refresh_from_db()
        self.assertEqual(order.total, D("100.00"))
        self.assertEqual(other.amount, D("100.00"))
        _, _, _, back, _ = change_payment_provider(order, self.provider, recreate_invoices=False)
        order.refresh_from_db()
        self.assertEqual(order.total, D("95.00"))
        self.assertEqual(back.amount, D("95.00"))
        self.assertEqual(order.fees.count(), 1)

    def test_tax_on_the_negative_adjustment_uses_pretix_tax_rule(self):
        TaxRule.objects.create(event=self.event, name="VAT", rate=D("20"), default=True)
        self.event.settings.set("tax_rule_payment", "default")
        order, payment = self.order()
        self.assertEqual(payment.fee.tax_rate, D("20"))
        self.assertEqual(payment.fee.tax_value, D("-0.83"))
        self.assertEqual(order.total, D("95.00"))

    def test_rehearsal_percentage_and_tiny_amount_remain_payable(self):
        self.provider.settings.set("subsidy_percent", "99.50")
        order, payment = self.order("280.00")
        self.assertEqual((order.total, payment.amount), (D("1.40"), D("1.40")))
        self.provider.settings.set("subsidy_percent", "99.99")
        tiny, payment = self.order("0.01")
        self.assertEqual((tiny.total, payment.amount), (D("0.01"), D("0.01")))

    def test_net_tax_rounding_preserves_a_payable_balance(self):
        tax = TaxRule.objects.create(event=self.event, name="VAT", rate=D("20"), default=True)
        self.item.tax_rule = tax
        self.item.save()
        self.event.settings.set("tax_rule_payment", "default")
        self.event.settings.set("tax_rounding", "sum_by_net")
        self.provider.settings.set("subsidy_percent", "99.99")
        order, payment = self.order()
        self.assertGreaterEqual(order.total, D("0.01"))
        self.assertEqual(order.total, payment.amount)

    def test_small_balances_across_pretix_tax_rounding_modes(self):
        tax = TaxRule.objects.create(event=self.event, name="VAT", rate=D("20"), default=True)
        self.event.__dict__["cached_default_tax_rule"] = tax
        self.event.settings.set("tax_rule_payment", "default")
        self.provider.settings.set("subsidy_percent", "99.99")
        for rate in ("0", "7", "10", "20", "27", "100"):
            tax.rate = D(rate)
            for mode in ("line", "sum_by_net", "sum_by_net_only_business", "sum_by_net_keep_gross"):
                self.event.settings.set("tax_rounding", mode)
                for price in ("0.01", "0.02", "0.03", "0.10", "1.00", "100.00"):
                    value = tax.tax(D(price), base_price_is="gross")
                    position = CartPosition(event=self.event, item=self.item, price=D(price),
                                              tax_rate=value.rate, tax_value=value.tax)
                    payment = {"provider": self.provider.identifier, "pprov": self.provider, "info_data": {}}
                    # pretix 2026.8 added this argument; 2026.7 still has the same rounding engine.
                    options = {"sales_channel": self.channel} if "sales_channel" in signature(_apply_rounding_and_fees).parameters else {}
                    fees = _apply_rounding_and_fees([position], [payment],
                                                    InvoiceAddress(country="AT", is_business=True),
                                                    {}, self.event, **options)
                    total = position.price + sum(f.value for f in fees)
                    with self.subTest(rate=rate, mode=mode, price=price):
                        self.assertGreaterEqual(total, D("0.01"))
                        self.assertEqual(payment["payment_amount"], total)

    def test_existing_order_keeps_a_positive_balance_after_event_rounding_changes(self):
        tax = TaxRule.objects.create(event=self.event, name="VAT", rate=D("20"), default=True)
        self.item.tax_rule = tax
        self.item.save()
        self.event.settings.set("tax_rule_payment", "default")
        self.event.settings.set("tax_rounding", "sum_by_net")
        order, _ = self.order()
        self.assertEqual(order.tax_rounding_mode, "sum_by_net")
        self.event.settings.set("tax_rounding", "line")
        self.provider.settings.set("subsidy_percent", "99.99")
        _, _, _, payment, _ = change_payment_provider(order, self.provider, recreate_invoices=False)
        order.refresh_from_db()
        self.assertGreaterEqual(order.total, D("0.01"))
        self.assertEqual(order.total, payment.amount)
