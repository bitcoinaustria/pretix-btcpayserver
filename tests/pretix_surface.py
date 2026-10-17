"""Security regressions against a test-mode pretix event; run in pretix shell, without BTCPay calls."""
import hashlib
import hmac
import json
import os
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderPayment

from pretix_btcpay import api, views
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
            event=EVENT_OBJECT, status=Order.STATUS_PENDING, total=Decimal('1.00'), testmode=True,
            expires=now() + timedelta(minutes=30), email=None, locale='en',
            sales_channel=EVENT_OBJECT.organizer.sales_channels.get(identifier='web'),
        )
        return order.payments.create(provider='btcpay_greenfield', amount=Decimal('1.00'), state=state,
                                     info=json.dumps(info or {}))

    def clean(self, **changes):
        data = {'url': 'https://pay.example.org', 'api_key': 'test-only-key', 'store_id': 'REVIEW'}
        data.update(changes)
        with patch('pretix_btcpay.payment.BTCPayAPI', return_value=self.client):
            return self.provider.settings_form_clean({'payment_btcpay_greenfield_' + k: v for k, v in data.items()})

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
        with patch.object(self.provider, 'ensure_webhook') as ensure:
            self.clean(url='https://new.example.org')
        ensure.assert_called_once()

    def test_same_store_key_rotation_is_allowed(self):
        self.make_payment({'invoice_id': 'EXISTING'})
        self.clean(api_key='replacement-test-only-key')
        self.client.current_key.assert_called_once()

    def test_signed_deep_json_is_bad_request(self):
        body = b'[' * 1200 + b'0' + b']' * 1200
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
