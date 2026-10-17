"""Concurrent real settings views on an isolated event; run in pretix shell, without BTCPay calls."""
import os
import threading
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection, connections, transaction
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderFee
from pretix.control.views.event import PaymentProviderSettings

from pretix_btcpay import sync
from pretix_btcpay.api import BTCPayAPI, WEBHOOK_EVENTS
from pretix_btcpay.checks import REQUIRED_PERMISSIONS
from pretix_btcpay.payment import BTCPayServer

ORGANIZER, EVENT = os.environ.get('CHECK_EVENT', 'zitadelle/2027').split('/')
with scopes_disabled():
    MAIN = Event.objects.get(organizer__slug=ORGANIZER, slug=EVENT)
if not MAIN.testmode or connection.vendor != 'postgresql':
    raise RuntimeError('These checks require a test-mode event and PostgreSQL.')


class SettingsSurfaceChecks(unittest.TestCase):
    def setUp(self):
        self.enterContext(scope(organizer=MAIN.organizer))
        self.event = Event.objects.create(
            organizer=MAIN.organizer, slug='review-b-settings-' + uuid.uuid4().hex[:12],
            name={'en': 'Reviewer B isolated settings'}, date_from=now(), currency=MAIN.currency,
            testmode=True, plugins='pretix_btcpay')
        self.addCleanup(self.remove_event)
        self.event.settings.set('locales', ['en'])
        self.prefix = 'payment_' + sync.PROVIDER + '_'
        for key, value in dict(url='https://pay.example.org', store_id='OLD-STORE', api_key='test-old-key',
                               webhook_id='OLD-WEBHOOK', webhook_secret='old-test-secret', pending_webhook='',
                               _enabled=True, _hidden_seed='review-only-seed', speed_policy='MediumSpeed',
                               invoice_expiration=15).items():
            self.event.settings.set(self.prefix + key, value)
        # First-time initialization of other providers must not serialize these requests through unrelated writes.
        self.event.get_payment_providers()
        self.old_client = self.client('https://pay.example.org', 'OLD-STORE', 'OLD-WEBHOOK')
        self.release = threading.Event()
        self.started = threading.Event()
        self.changed = threading.Event()
        self.results, self.errors, self.workers = {}, [], []
        self.addCleanup(self.finish_workers)

    def remove_event(self):
        with transaction.atomic():
            Order.gracefully_delete_bulk(self.event, Order.objects.filter(event=self.event, testmode=True))
            self.event.delete()

    def finish_workers(self):
        self.release.set()
        for worker in self.workers:
            worker.join(25)
            self.assertFalse(worker.is_alive(), 'Settings worker did not finish')

    def client(self, server, store, webhook_id):
        client = Mock(spec=BTCPayAPI)
        client.url = server
        client.current_key.return_value = {'permissions': [p + ':' + store for p in REQUIRED_PERMISSIONS]}
        webhook = {'id': webhook_id, 'url': BTCPayServer(self.event).webhook_url, 'enabled': True,
                   'automaticRedelivery': True, 'authorizedEvents': {'specificEvents': WEBHOOK_EVENTS}}
        client.get_webhook.return_value = webhook
        client.list_webhooks.return_value = [webhook]
        client.create_webhook.return_value = webhook
        client.update_webhook.return_value = webhook
        return client

    def save_view(self, label, **changes):
        try:
            with scope(organizer=MAIN.organizer):
                event = Event.objects.get(pk=self.event.pk)
                data = dict(url='https://pay.example.org', store_id='OLD-STORE', api_key='test-old-key',
                            _enabled='on', speed_policy='MediumSpeed', invoice_expiration=16,
                            _restrict_to_sales_channels=['web'])
                data.update(changes)
                request = RequestFactory().post('/settings/payment/', {self.prefix + k: v for k, v in data.items()})
                request.event, request.organizer, request.user = event, MAIN.organizer, None
                request.session = {}
                request._messages = FallbackStorage(request)
                view = PaymentProviderSettings()
                view.setup(request, organizer=MAIN.organizer.slug, event=event.slug, provider=sync.PROVIDER)
                view.get = lambda request: HttpResponse(str(view.form.errors), status=400)
                response = view.post(request)
                self.results[label] = (response.status_code, str(view.form.errors))
        except Exception as error:
            self.errors.append(error)
        finally:
            if label == 'switch':
                self.changed.set()
            connections.close_all()

    def start(self, label, **changes):
        worker = threading.Thread(target=self.save_view, args=(label,), kwargs=changes,
                                  name='review-b-' + label, daemon=True)
        self.workers.append(worker)
        worker.start()

    def make_invoice(self, server, store):
        event = Event.objects.get(pk=self.event.pk)
        with transaction.atomic():
            order = Order.objects.create(event=event, status=Order.STATUS_PENDING, testmode=True, total=Decimal('1.00'),
                                         expires=now() + timedelta(minutes=30), locale='en', email=None,
                                         sales_channel=MAIN.organizer.sales_channels.get(identifier='web'))
            order.fees.create(fee_type=OrderFee.FEE_TYPE_OTHER, value=Decimal('1.00'), description='Review settings fixture')
            order.create_transactions()
            payment = order.payments.create(provider=sync.PROVIDER, amount=Decimal('1.00'), state='created', info='{}')
        provider = BTCPayServer(event)
        client = self.client(server, store, 'NEW-WEBHOOK')
        client.create_invoice.return_value = {'id': 'NEW-INVOICE', 'status': 'New',
            'checkoutLink': server + '/i/NEW-INVOICE',
            'expirationTime': int((now() + timedelta(minutes=15)).timestamp())}
        provider.__dict__['client'] = client
        provider.execute_payment(None, payment)
        client.create_invoice.assert_called_once()
        payment.refresh_from_db()
        return payment

    def pause_key(self):
        self.started.set()
        if not self.release.wait(15):
            raise AssertionError('Old settings request was not released')
        return {'permissions': [p + ':OLD-STORE' for p in REQUIRED_PERMISSIONS]}

    def overlapping_save(self, *, server, store, disabled_alias=False):
        new_client = self.client(server, store, 'NEW-WEBHOOK')
        fields = BTCPayServer.settings_form_fields.fget

        def slow_fields(provider):
            result = fields(provider)
            if disabled_alias and threading.current_thread().name == 'review-b-old':
                validate = result['url'].validate

                def slow_validate(value):
                    self.started.set()
                    if not self.release.wait(15):
                        raise AssertionError('Old settings request was not released')
                    validate(value)

                result['url'].validate = slow_validate
            return result

        self.old_client.current_key.side_effect = self.pause_key
        with patch('pretix_btcpay.payment.BTCPayAPI', side_effect=lambda **kw:
                   self.old_client if kw['api_key'] == 'test-old-key' else new_client), \
                patch.object(BTCPayServer, 'settings_form_fields', property(slow_fields)):
            self.start('old', **({'_enabled': '', 'url': 'https://pay.example.org/'} if disabled_alias else {}))
            self.assertTrue(self.started.wait(10),
                            f'Old settings request never reached its pause: {self.errors!r}, {self.results!r}')
            self.start('switch', url=server, store_id=store, api_key='test-new-key', invoice_expiration=17)
            switched = self.changed.wait(3)
            payment = self.make_invoice(server, store) if switched else None
            self.release.set()
            self.finish_workers()
            self.assertFalse(self.errors, self.errors)
            self.assertEqual(self.results['old'][0], 400 if disabled_alias else 302, self.results)
            if disabled_alias:
                self.assertTrue(any(text in self.results['old'][1]
                                    for text in ('invoices of this event', 'Rechnungen dieses Events')), self.results)
            self.assertEqual(self.results['switch'][0], 302, self.results)
            payment = payment or self.make_invoice(server, store)
        current = BTCPayServer(Event.objects.get(pk=self.event.pk))
        self.assertTrue(sync.same_origin(payment.info_data, sync.invoice_origin(current)),
                        'An unchanged settings request restored the old connection after a new invoice was created')

    def test_unchanged_settings_cannot_undo_a_store_change(self):
        self.overlapping_save(server='https://pay.example.org', store='NEW-STORE')

    def test_unchanged_settings_cannot_undo_a_server_change(self):
        self.overlapping_save(server='https://new.example.org', store='OLD-STORE')

    def test_disabled_alias_edit_checks_the_connection_after_a_concurrent_change(self):
        self.overlapping_save(server='https://new.example.org', store='OLD-STORE', disabled_alias=True)

    def test_unchanged_settings_leave_checkouts_concurrent(self):
        self.old_client.current_key.side_effect = self.pause_key
        with patch('pretix_btcpay.payment.BTCPayAPI', return_value=self.old_client):
            self.start('old')
            self.assertTrue(self.started.wait(10), self.errors)
            payment = self.make_invoice('https://pay.example.org', 'OLD-STORE')
            self.release.set()
            self.finish_workers()
        self.assertFalse(self.errors, self.errors)
        self.assertEqual(self.results['old'][0], 302, self.results)
        self.assertTrue(sync.same_origin(payment.info_data, sync.invoice_origin(BTCPayServer(
            Event.objects.get(pk=self.event.pk)))))


suite = unittest.defaultTestLoader.loadTestsFromTestCase(SettingsSurfaceChecks)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise SystemExit(1)
