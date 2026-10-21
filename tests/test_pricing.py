"""Money and rounding regressions without Django or a running pretix."""
import unittest
from decimal import Decimal as D

from pretix_btcpay.pricing import subsidized_fee


class SubsidyTest(unittest.TestCase):
    def test_disabled_preserves_existing_positive_and_negative_fees(self):
        for fee in ('0', '2.64', '-5.00', '-100.00'):
            self.assertEqual(subsidized_fee(D('100'), D(fee), D('0'), 2), D(fee))

    def test_percentage_applies_to_the_remaining_gross_balance(self):
        self.assertEqual(subsidized_fee(D('100'), D('0'), D('5'), 2), D('-5.00'))
        self.assertEqual(subsidized_fee(D('60'), D('0'), D('5'), 2), D('-3.00'))
        self.assertEqual(subsidized_fee(D('280'), D('0'), D('99.50'), 2), D('-278.60'))

    def test_half_cent_rounds_up_and_existing_fee_is_preserved(self):
        self.assertEqual(subsidized_fee(D('45'), D('0'), D('0.5'), 2), D('-0.23'))
        self.assertEqual(subsidized_fee(D('100'), D('2.64'), D('5'), 2), D('-2.36'))

    def test_high_discount_leaves_an_actual_payment(self):
        self.assertEqual(subsidized_fee(D('0.01'), D('0'), D('99.99'), 2), D('0.00'))
        self.assertEqual(subsidized_fee(D('1'), D('-0.90'), D('99.99'), 2), D('-0.97'))
        self.assertEqual(subsidized_fee(D('0'), D('0'), D('99.99'), 2), D('0'))
        self.assertEqual(subsidized_fee(D('100'), D('0'), D('99.99'), 2), D('-99.97'))

    def test_currency_precision(self):
        self.assertEqual(subsidized_fee(D('100'), D('0'), D('5.5'), 0), D('-6'))
        self.assertEqual(subsidized_fee(D('1.001'), D('0'), D('5'), 3), D('-0.050'))

    def test_subsidy_never_increases_the_fee_or_consumes_the_whole_balance(self):
        for cents in range(1, 10001, 7):
            price = D(cents) / 100
            for percent in ('0.01', '5', '99.50', '99.99'):
                fee = subsidized_fee(price, D('0.25'), D(percent), 2)
                with self.subTest(cents=cents, percent=percent):
                    self.assertGreaterEqual(price + fee, D('0.01'))
                    self.assertLessEqual(fee, D('0.25'))
                    self.assertEqual(fee, fee.quantize(D('0.01')))


if __name__ == '__main__':
    unittest.main()
