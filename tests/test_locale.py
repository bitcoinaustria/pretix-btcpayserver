"""Keep payment recovery instructions translated in both shipped German catalogs."""
import ast
import gettext
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'pretix_btcpay' / 'locale'
RECOVERY = (
    'Complete payment',
    'Your payment has arrived, but not the full amount yet — it may have been replaced with a cheaper transaction. '
    'Please complete the payment: the remaining amount is shown on the payment page.',
)
CONNECTION = (
    "This payment's invoice was created in another BTCPay store or on another server than the one configured now. "
    'Check it there; pretix cannot see it any more.',
    '{count} BTCPay invoices of this event can still be paid or are not booked yet. Change the server or store '
    'only after they have run out or are booked.',
)


def read_po(path):
    entries, entry, field = {}, {}, None
    for line in path.read_text().splitlines() + ['']:
        if line.startswith(('msgid ', 'msgstr ')):
            field, value = line.split(' ', 1)
            entry[field] = ast.literal_eval(value)
        elif line.startswith('"'):
            entry[field] += ast.literal_eval(line)
        elif not line.strip() and entry:
            entries[entry['msgid']] = entry.get('msgstr', '')
            entry, field = {}, None
    return entries


class LocaleTest(unittest.TestCase):
    def test_connection_warnings_are_translated_with_the_count_placeholder(self):
        for locale in ('de', 'de_Informal'):
            with (ROOT / locale / 'LC_MESSAGES' / 'django.mo').open('rb') as source:
                translations = gettext.GNUTranslations(source)
            for message in CONNECTION:
                with self.subTest(locale=locale, message=message):
                    translated = translations.gettext(message)
                    self.assertNotEqual(translated, message)
                    self.assertEqual(translated.count('{count}'), message.count('{count}'))
                    self.assertNotIn('{count}', translated.format(count=3))

    def test_recovery_instructions_are_translated(self):
        for locale in ('de', 'de_Informal'):
            with (ROOT / locale / 'LC_MESSAGES' / 'django.mo').open('rb') as source:
                translations = gettext.GNUTranslations(source)
            for message in RECOVERY:
                with self.subTest(locale=locale, message=message):
                    self.assertNotEqual(translations.gettext(message), message)

    def test_compiled_catalogs_match_sources(self):
        for locale in ('de', 'de_Informal'):
            directory = ROOT / locale / 'LC_MESSAGES'
            with (directory / 'django.mo').open('rb') as source:
                translations = gettext.GNUTranslations(source)
            for message, translated in read_po(directory / 'django.po').items():
                if message and translated:
                    with self.subTest(locale=locale, message=message):
                        self.assertEqual(translations.gettext(message), translated)


if __name__ == '__main__':
    unittest.main()
