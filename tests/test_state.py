"""The rules in pretix_btcpay/state.py, without pretix: python -m unittest discover -s tests -t ."""
import unittest
from datetime import datetime, timedelta, timezone

from pretix_btcpay import state
from pretix_btcpay.state import CANCELED, CONFIRMED, CREATED, FAILED, PENDING, REFUNDED, decide

STATES = (CREATED, PENDING, CONFIRMED, FAILED, CANCELED, REFUNDED)
NOW = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)


class DecideTest(unittest.TestCase):
    def test_new_never_changes_the_payment(self):
        for s in STATES:
            for extra in (None, "None", "PaidPartial"):
                self.assertEqual(decide("New", extra, s).action, "none", (s, extra))

    def test_processing_is_pending_and_keeps_the_order(self):
        self.assertEqual(decide("Processing", "None", CREATED).action, "pending")
        for s in (PENDING, FAILED, CANCELED):
            self.assertEqual(decide("Processing", "None", s).action, "hold", s)
        for s in (CONFIRMED, REFUNDED):
            self.assertEqual(decide("Processing", "None", s).action, "none", s)

    def test_zero_confirmations_never_confirm(self):
        for s in STATES:
            for extra in (None, "None", "PaidOver"):
                self.assertNotEqual(decide("Processing", extra, s).action, "confirm", (s, extra))

    def test_settled_confirms_from_every_open_or_given_up_state(self):
        for s in (CREATED, PENDING, FAILED, CANCELED):
            self.assertEqual(decide("Settled", "None", s).action, "confirm", s)
        self.assertEqual(decide("Settled", "None", CONFIRMED).action, "none")

    def test_settled_after_refund_is_only_noted(self):
        d = decide("Settled", "None", REFUNDED)
        self.assertEqual(d.action, "none")
        self.assertIn(state.NOTE_SETTLED_AFTER_REFUND, d.notes)

    def test_expired_and_invalid_fail_open_payments(self):
        for status in ("Expired", "Invalid"):
            for s in (CREATED, PENDING):
                self.assertEqual(decide(status, "None", s).action, "fail", (status, s))
            for s in (FAILED, CANCELED, REFUNDED):
                self.assertEqual(decide(status, "None", s).action, "none", (status, s))

    def test_invalid_after_confirmation_is_for_a_human(self):
        d = decide("Invalid", "Marked", CONFIRMED)
        self.assertEqual(d.action, "none")
        self.assertIn(state.NOTE_INVALID_AFTER_CONFIRM, d.notes)
        self.assertEqual(decide("Expired", "None", CONFIRMED).action, "none")

    def test_notes(self):
        self.assertEqual(decide("Expired", "PaidPartial", CREATED), state.Decision("fail", (state.NOTE_PARTIAL,)))
        self.assertEqual(decide("Expired", "PaidLate", CREATED), state.Decision("fail", (state.NOTE_LATE,)))
        self.assertEqual(decide("Settled", "PaidOver", CREATED), state.Decision("confirm", (state.NOTE_OVERPAID,)))
        self.assertEqual(decide("Settled", "Marked", FAILED), state.Decision("confirm", (state.NOTE_MARKED,)))

    def test_idempotent(self):
        after = {"pending": PENDING, "hold": None, "confirm": CONFIRMED, "fail": FAILED, "none": None}
        for status in state.KNOWN_STATUSES:
            for extra in ("None", "PaidPartial", "PaidOver", "PaidLate", "Marked"):
                for s in STATES:
                    first = decide(status, extra, s)
                    new_state = after[first.action] or s
                    second = decide(status, extra, new_state)
                    self.assertIn(second.action, ("none", "hold", "pending") if status == "Processing" else ("none",),
                                  (status, extra, s, first, second))
                    if status == "Processing" and new_state == PENDING:
                        self.assertEqual(second.action, "hold")

    def test_unknown_status_waits(self):
        for s in STATES:
            self.assertEqual(decide("Something", "None", s).action, "none")
            self.assertEqual(decide(None, None, s).action, "none")


class TimesTest(unittest.TestCase):
    def test_invoice_minutes(self):
        self.assertEqual(state.invoice_minutes(NOW + timedelta(minutes=30), NOW, 15), 15)
        self.assertEqual(state.invoice_minutes(NOW + timedelta(minutes=9, seconds=50), NOW, 15), 9)
        self.assertEqual(state.invoice_minutes(NOW + timedelta(seconds=20), NOW, 15), 1, "BTCPay needs at least a minute")
        self.assertEqual(state.invoice_minutes(NOW - timedelta(minutes=5), NOW, 15), 1)
        self.assertEqual(state.invoice_minutes(None, NOW, 15), 15)
        self.assertEqual(state.invoice_minutes(NOW + timedelta(hours=5), NOW, 0), 1, "a broken setting still gives a valid invoice")

    def test_hold_until(self):
        monitoring = NOW + timedelta(hours=24)
        self.assertEqual(state.hold_until(monitoring, NOW, timedelta(hours=24)), monitoring + timedelta(hours=1))
        self.assertEqual(state.hold_until(None, NOW, timedelta(hours=24)), NOW + timedelta(hours=25))
        self.assertEqual(state.hold_until(NOW - timedelta(hours=1), NOW, timedelta(hours=2)), NOW + timedelta(hours=3))

    def test_is_final(self):
        self.assertTrue(state.is_final("Settled", None, NOW))
        self.assertTrue(state.is_final("Invalid", None, NOW))
        self.assertFalse(state.is_final("Expired", NOW + timedelta(minutes=5), NOW), "a late payment may still arrive")
        self.assertTrue(state.is_final("Expired", NOW - timedelta(minutes=5), NOW))
        self.assertFalse(state.is_final("New", None, NOW))
        self.assertFalse(state.is_final("Processing", None, NOW))
        self.assertFalse(state.is_final(None, None, NOW))


if __name__ == "__main__":
    unittest.main()
