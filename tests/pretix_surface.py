"""Security regressions against a test-mode pretix event; run in pretix shell, without BTCPay calls."""
import hashlib
import hmac
import json
import os
import unittest
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import RequestFactory
from django.utils.timezone import now
from django.utils.translation import override
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderPayment
from pretix.control.forms.event import ProviderForm

from pretix_btcpay import api, state as invoice_state, sync, views
from pretix_btcpay.api import BTCPayAPI, BTCPayError, WEBHOOK_EVENTS
from pretix_btcpay.checks import REQUIRED_PERMISSIONS
from pretix_btcpay.payment import BTCPayServer

ORGANIZER, EVENT = os.environ.get('CHECK_EVENT', 'zitadelle/2027').split('/')
with scopes_disabled():
    EVENT_OBJECT = Event.objects.get(organizer__slug=ORGANIZER, slug=EVENT)
if not EVENT_OBJECT.testmode or connection.vendor != 'postgresql':
    raise RuntimeError('These checks require a test-mode event and PostgreSQL.')


class SurfaceChecks(unittest.TestCase):
    def setUp(self):
        self.enterContext(scope(organizer=EVENT_OBJECT.organizer))
        self.enterContext(transaction.atomic())
        self.addCleanup(EVENT_OBJECT.settings.flush)
        self.addCleanup(transaction.set_rollback, True)
        self.provider = BTCPayServer(EVENT_OBJECT)
        for key, value in dict(url='https://pay.example.org', api_key='test-only-key', store_id='REVIEW',
                               webhook_id='WEBHOOK', webhook_secret='old-test-secret', pending_webhook='').items():
            self.provider.settings.set(key, value)
        self.client = Mock(spec=BTCPayAPI)
        self.client.url = 'https://pay.example.org'
        self.client.current_key.return_value = {'permissions': [p + ':REVIEW' for p in REQUIRED_PERMISSIONS]}
        self.webhook = {'id': 'WEBHOOK', 'url': self.provider.webhook_url, 'enabled': True,
                        'automaticRedelivery': True,
                        'authorizedEvents': {'everything': False, 'specificEvents': WEBHOOK_EVENTS}}
        self.client.get_webhook.return_value = self.webhook
        self.client.list_webhooks.return_value = [self.webhook]
        self.client.update_webhook.return_value = self.webhook
        self.client.create_webhook.return_value = self.webhook
        self.provider.__dict__['client'] = self.client

    def make_payment(self, info=None, state='created'):
        order = Order.objects.create(
            event=self.provider.event, status=Order.STATUS_PENDING, total=Decimal('1.00'), testmode=True,
            expires=now() + timedelta(minutes=30), email=None, locale='en',
            sales_channel=EVENT_OBJECT.organizer.sales_channels.get(identifier='web'),
        )
        return order.payments.create(provider='btcpay_greenfield', amount=Decimal('1.00'), state=state,
                                     info=json.dumps(info or {}))

    def isolate_event(self):
        # Settings changes must be checked against only our invoices, not a previous regtest run's orders.
        event = Event.objects.create(organizer=EVENT_OBJECT.organizer, slug='review-c-store-switch',
                                     name='Store switch regression', date_from=now(), testmode=True,
                                     currency=EVENT_OBJECT.currency)
        self.provider = BTCPayServer(event)
        for key, value in dict(url='https://pay.example.org', api_key='test-only-key', store_id='REVIEW',
                               webhook_id='WEBHOOK', webhook_secret='old-test-secret').items():
            self.provider.settings.set(key, value)
        self.provider.__dict__['client'] = self.client

    def clean(self, **changes):
        data = {'url': 'https://pay.example.org', 'api_key': 'test-only-key', 'store_id': 'REVIEW'}
        data.update(changes)
        with patch('pretix_btcpay.payment.BTCPayAPI', return_value=self.client):
            return self.provider.settings_form_clean({'payment_btcpay_greenfield_' + k: v for k, v in data.items()})

    def settings_form(self, enabled=True, **changes):
        prefix = self.provider.settings.get_prefix()
        data = {'url': 'https://pay.example.org', 'api_key': 'test-only-key', 'store_id': 'REVIEW',
                'speed_policy': 'MediumSpeed', '_restrict_to_sales_channels': ['web']}
        if enabled:
            data['_enabled'] = 'on'
        data.update(changes)
        form = ProviderForm(obj=EVENT_OBJECT, settingspref=prefix, provider=self.provider,
                            data={prefix + k: v for k, v in data.items()})
        form.fields = OrderedDict((prefix + k, v) for k, v in self.provider.settings_form_fields.items())
        form.prepare_fields()
        return form

    def signed_request(self, body):
        request = RequestFactory().post('/webhook/', data=body, content_type='application/json',
            HTTP_BTCPAY_SIG='sha256=' + hmac.new(b'old-test-secret', body, hashlib.sha256).hexdigest())
        request.event = EVENT_OBJECT
        return request

    def test_failed_alias_edit_keeps_the_working_webhook_secret(self):
        self.client.url = 'https://PAY.example.org'
        with patch('pretix_btcpay.payment.secrets.token_urlsafe', return_value='new-test-secret'):
            self.clean(url=self.client.url)
        # Another settings field failed, so the new URL is never saved.
        sent = self.client.update_webhook.call_args
        remote_secret = sent.kwargs['secret'] if sent else 'old-test-secret'
        self.assertIn(remote_secret, self.provider.webhook_secrets())

    def test_existing_webhook_has_redelivery_repaired(self):
        self.webhook['automaticRedelivery'] = False
        self.clean()
        self.client.update_webhook.assert_called_once()

    def test_connection_change_before_first_invoice_is_allowed(self):
        # The shared check event can hold open invoices of other checks; this one is about an event without any.
        with patch.object(self.provider, 'ensure_webhook') as ensure, patch('pretix_btcpay.payment.still_open', return_value=False):
            self.clean(url='https://new.example.org')
        ensure.assert_called_once()

    def test_same_store_key_rotation_is_allowed(self):
        self.make_payment({'invoice_id': 'EXISTING'})
        self.clean(api_key='replacement-test-only-key')
        self.client.current_key.assert_called_once()

    def test_disabled_provider_cannot_change_or_clear_an_open_invoice_connection(self):
        self.make_payment({'invoice_id': 'EXISTING', 'status': 'New'})
        # pretix skips settings_form_clean when disabled; field validation must still protect outstanding invoices.
        for changes in ({'store_id': 'OTHER'}, {'url': 'https://other.example.org'},
                        {'store_id': ''}, {'url': ''}):
            with self.subTest(changes=changes):
                form = self.settings_form(enabled=False, **changes)
                self.assertFalse(form.is_valid())
                self.assertTrue(any('BTCPay invoices of this event' in message or 'BTCPay-Rechnungen dieses Events' in message
                                    for messages in form.errors.values() for message in messages))
        self.client.current_key.assert_not_called()
        self.client.update_webhook.assert_not_called()

    def test_disabling_without_changing_the_connection_remains_allowed(self):
        self.make_payment({'invoice_id': 'EXISTING', 'status': 'Processing'})
        with patch.object(self.provider, 'settings_form_clean') as clean:
            form = self.settings_form(enabled=False)
            self.assertTrue(form.is_valid(), form.errors)
            clean.assert_not_called()
        self.client.current_key.assert_not_called()

    def test_disabled_connection_can_change_after_all_invoices_are_final(self):
        # Other test runs may have open invoices on the shared event; simulate a fully reconciled event here.
        with patch('pretix_btcpay.payment.still_open', return_value=False):
            form = self.settings_form(enabled=False, url='https://other.example.org', store_id='OTHER')
            self.assertTrue(form.is_valid(), form.errors)
        self.client.current_key.assert_not_called()

    def test_real_settings_form_accepts_normalized_aliases_with_open_invoices(self):
        self.make_payment({'invoice_id': 'EXISTING', 'status': 'New'})
        for alias in ('HTTPS://PAY.EXAMPLE.ORG:443/', 'https://PAY.example.org/', 'https://pay.example.org:443'):
            with self.subTest(alias=alias), patch('pretix_btcpay.payment.BTCPayAPI', return_value=self.client):
                form = self.settings_form(url=alias)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(self.provider.webhook_secrets(), ['old-test-secret'])
        self.client.update_webhook.assert_not_called()

    def test_store_switch_guard_uses_the_invoice_state_and_monitoring_deadline(self):
        self.isolate_event()
        at = now()
        for status, payment_state, monitoring_hours, order_paid, blocked in (
                ('New', 'created', 1, False, True), ('Processing', 'pending', 1, False, True),
                ('Settled', 'failed', -1, False, True), ('Settled', 'canceled', -1, False, True),
                ('Expired', 'failed', 1, False, True), ('Expired', 'failed', -1, False, False),
                ('Invalid', 'failed', 1, False, False), ('Settled', 'confirmed', 1, True, False),
                # Confirmed, but the order is not marked paid yet: reconciliation still has to finish it.
                ('Settled', 'confirmed', 1, False, True)):
            with self.subTest(status=status, payment_state=payment_state, monitoring_hours=monitoring_hours,
                              order_paid=order_paid):
                payment = self.make_payment({'invoice_id': 'EXISTING', 'status': status,
                    'monitoring_until': int((at + timedelta(hours=monitoring_hours)).timestamp())}, state=payment_state)
                if order_paid:
                    Order.objects.filter(pk=payment.order_id).update(status=Order.STATUS_PAID)
                    payment.refresh_from_db()
                with patch.object(self.provider, 'ensure_webhook'), override('en'):
                    if blocked:
                        with self.assertRaisesRegex(ValidationError, 'BTCPay invoices of this event'):
                            self.clean(url='https://new.example.org')
                    else:
                        self.clean(url='https://new.example.org')
                Order.gracefully_delete_bulk(self.provider.event, Order.objects.filter(pk=payment.order_id))

    def test_other_server_skips_api_updates_checked_and_logs_once(self):
        payment = self.make_payment({'invoice_id': 'EXISTING', 'status': 'New',
            'server': 'https://old.example.org', 'store_id': 'REVIEW'})
        for _ in range(2):
            sync.sync_payment(self.provider, payment, 'regression')
        payment.refresh_from_db()
        self.client.get_invoice.assert_not_called()
        self.assertEqual(payment.state, 'created')
        self.assertGreater(payment.info_data['checked'], 0)
        self.assertEqual(payment.order.all_logentries().filter(action_type='pretix_btcpay.other_store').count(), 1)
        self.assertTrue(sync.same_origin({'server': 'HTTPS://PAY.example.org:443/', 'store_id': 'REVIEW'},
                                        sync.invoice_origin(self.provider)))

    def test_store_switch_is_refused_while_settled_invoice_has_unbooked_surplus(self):
        self.isolate_event()
        payment = self.make_payment({'invoice_id': 'EXISTING', **sync.invoice_origin(self.provider)}, state='confirmed')
        order = payment.order
        order.status = Order.STATUS_PAID
        order.save(update_fields=['status'])
        invoice = {'id': 'EXISTING', 'storeId': 'REVIEW', 'amount': '1.00', 'currency': self.provider.event.currency,
            'status': invoice_state.SETTLED, 'additionalStatus': 'PaidOver', 'paidAmount': '2.00',
            'checkout': {'speedPolicy': 'MediumSpeed'},
            'monitoringExpiration': int((now() + timedelta(hours=1)).timestamp()),
            'metadata': {'pretixPaymentId': payment.pk, 'orderId': order.code,
                         'pretixEvent': sync.event_ref(self.provider.event)}}
        # The base transfer has settled; a later transfer is visible but must not be booked before confirmation.
        self.client.get_invoice_payment_methods.return_value = [{'rate': '1', 'payments': [
            {'status': 'Settled', 'value': '1.00'}, {'status': 'Processing', 'value': '1.00'}]}]
        sync.apply(self.provider, payment, invoice, 'regression')
        payment.refresh_from_db()
        self.assertTrue(payment.info_data['surplus_open'])
        self.assertEqual(order.payments.count(), 1)
        self.assertIsNotNone(sync.next_check(payment, now()))
        with patch.object(self.provider, 'ensure_webhook'), override('en'):
            with self.assertRaisesRegex(ValidationError, 'BTCPay invoices of this event'):
                self.clean(url='https://new.example.org')

    def test_signed_deep_json_is_bad_request(self):
        body = b'[' * 20000 + b'0' + b']' * 20000
        with patch.object(views, '_provider', return_value=self.provider):
            response = views.webhook(self.signed_request(body))
        self.assertEqual(response.status_code, 400)
        self.client.get_invoice.assert_not_called()

    def test_malformed_invoice_metadata_returns_retryable_response(self):
        body = b'{"type":"InvoiceSettled","invoiceId":"X","storeId":"REVIEW"}'
        self.provider.__dict__['client'] = BTCPayAPI('https://pay.example.org', 'test-key')
        for metadata in ([], ['bad'], 'bad', 12):
            with self.subTest(metadata=metadata), patch.object(BTCPayAPI, '_request', return_value={'metadata': metadata}):
                with patch.object(views, '_provider', return_value=self.provider):
                    response = views.webhook(self.signed_request(body))
                self.assertEqual(response.status_code, 503)

    def test_malformed_api_lists_raise_provider_error(self):
        client = BTCPayAPI('https://pay.example.org', 'test-key')
        for data in ([None], ['bad'], [12]):
            with self.subTest(data=data), patch.object(client, '_request', return_value=data):
                for call in (lambda: client.list_webhooks('REVIEW'),
                             lambda: client.get_invoice_payment_methods('REVIEW', 'X')):
                    with self.assertRaises(BTCPayError):
                        call()
        for data in ([{'payments': 'bad'}], [{'payments': [None]}], [{'payments': [1]}]):
            with self.subTest(data=data), patch.object(client, '_request', return_value=data):
                with self.assertRaises(BTCPayError):
                    client.get_invoice_payment_methods('REVIEW', 'X')

    def test_signature_is_verified_before_json_and_api(self):
        request = self.signed_request(b'{')
        request.META['HTTP_BTCPAY_SIG'] = 'sha256=forged'
        with patch.object(views, '_provider', return_value=self.provider), patch.object(views.json, 'loads') as loads:
            response = views.webhook(request)
        self.assertEqual(response.status_code, 400)
        loads.assert_not_called()
        self.client.get_invoice.assert_not_called()

    def test_status_requires_the_right_order_secret_and_payment(self):
        payment = self.make_payment({'status': 'Processing'})
        other = self.make_payment()
        request = RequestFactory().get('/status/')
        request.event = EVENT_OBJECT
        for secret, ident, expected in [('wrong', payment.pk, 404), (payment.order.secret, other.pk, 404),
                                        (payment.order.secret, payment.pk, 200),
                                        (payment.order.secret, '9' * 100, 404)]:
            with self.subTest(expected=expected, ident=ident):
                response = views.status(request, order=payment.order.code, secret=secret, payment=ident)
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    self.assertEqual(response['Cache-Control'], 'no-store')
                    self.assertEqual(json.loads(response.content)['invoice_status'], 'Processing')
        self.client.get_invoice.assert_not_called()

    def test_api_requests_do_not_follow_redirects_and_have_timeouts(self):
        response = Mock(status_code=302, content=b'')
        response.text = 'redirect'
        with patch.object(api, '_session') as session:
            session.return_value.request.return_value = response
            with self.assertRaises(BTCPayError):
                BTCPayAPI('https://pay.example.org', 'test-key').current_key()
            call = session.return_value.request.call_args
            self.assertIs(call.kwargs['allow_redirects'], False)
            self.assertEqual(call.kwargs['timeout'], (5, 15))

    def test_api_wraps_json_recursion_errors(self):
        response = Mock(status_code=200, content=b'not-empty')
        response.json.side_effect = RecursionError('nested JSON')
        with patch.object(api, '_session') as session:
            session.return_value.request.return_value = response
            with self.assertRaises(BTCPayError):
                BTCPayAPI('https://pay.example.org', 'test-key').current_key()

    def test_api_wraps_timeouts_and_unexpected_shapes(self):
        client = BTCPayAPI('https://pay.example.org', 'test-key')
        with patch.object(api, '_session') as session:
            session.return_value.request.side_effect = requests.Timeout()
            with self.assertRaises(BTCPayError):
                client.current_key()
        for bad in (None, [], 'bad'):
            with self.subTest(bad=bad), patch.object(client, '_request', return_value=bad):
                with self.assertRaises(BTCPayError):
                    client.current_key()

    def test_control_markup_escapes_btcpay_data(self):
        payment = self.make_payment({'invoice_id': '<script>alert(1)</script>'})
        self.client.get_invoice.return_value = {'status': '<img src=x onerror=alert(1)>', 'amount': '<b>1</b>'}
        with patch('pretix_btcpay.payment.cache') as cache:
            cache.get.return_value = None
            markup = self.provider.payment_control_render(RequestFactory().get('/'), payment)
        self.assertNotIn('<script>alert(1)</script>', markup)
        self.assertNotIn('<img src=x onerror=alert(1)>', markup)
        self.assertIn('&lt;script&gt;', markup)

    def test_control_cache_is_scoped_to_the_store_and_event(self):
        payment = self.make_payment({'invoice_id': 'SAME'})
        with patch('pretix_btcpay.payment.cache') as cache:
            cache.get.return_value = None
            self.provider.payment_control_render(RequestFactory().get('/'), payment)
            key = cache.get.call_args.args[0]
            self.provider.settings.set('store_id', 'OTHER')
            self.provider.payment_control_render(RequestFactory().get('/'), payment)
            self.assertNotEqual(key, cache.get.call_args.args[0])


result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SurfaceChecks))
if not result.wasSuccessful():
    raise AssertionError('Payment surface checks failed')
