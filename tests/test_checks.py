"""The security checks in pretix_btcpay/checks.py, without pretix: python -m unittest discover -s tests -t ."""
import hashlib
import hmac
import unittest

from pretix_btcpay.checks import is_own_link, missing_permissions, verify_signature

SECRET = "s3cr3t-webhook-key"
BODY = b'{"type":"InvoiceSettled","invoiceId":"ABC","storeId":"STORE"}'


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SignatureTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(verify_signature(SECRET, BODY, sign(BODY)))
        self.assertTrue(verify_signature(SECRET, BODY, sign(BODY).upper().replace("SHA256=", "sha256=")))

    def test_rejects(self):
        self.assertFalse(verify_signature(SECRET, BODY, None))
        self.assertFalse(verify_signature(SECRET, BODY, ""))
        self.assertFalse(verify_signature(SECRET, BODY, sign(BODY)[7:]), "without the sha256= prefix")
        self.assertFalse(verify_signature(SECRET, BODY + b" ", sign(BODY)), "body changed")
        self.assertFalse(verify_signature(SECRET, BODY, sign(BODY, "other")), "other secret")
        self.assertFalse(verify_signature("", BODY, sign(BODY, "")), "no secret configured")
        self.assertFalse(verify_signature(SECRET, BODY, ["sha256=x"]))
        self.assertFalse(verify_signature(SECRET, BODY, "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()))


class LinkTest(unittest.TestCase):
    def test_own(self):
        self.assertTrue(is_own_link("https://pay.example.org", "https://pay.example.org/i/ABC"))
        self.assertTrue(is_own_link("http://localhost:8348/", "http://localhost:8348/invoice?id=1"))

    def test_foreign(self):
        for link in ("https://evil.example/i/ABC", "http://pay.example.org/i/ABC", "https://pay.example.org.evil/i",
                     "javascript:alert(1)", "//evil.example/", "", "https://pay.example.org@evil.example/"):
            self.assertFalse(is_own_link("https://pay.example.org", link), link)


class PermissionTest(unittest.TestCase):
    def test_exact(self):
        perms = ["btcpay.store.cancreateinvoice:S", "btcpay.store.canviewinvoices:S", "btcpay.store.webhooks.canmodifywebhooks:S"]
        self.assertEqual(missing_permissions(perms, "S"), ([], ["btcpay.store.canmodifyinvoices", "btcpay.store.cancreatepullpayments"], []))

    def test_other_store_does_not_count(self):
        perms = ["btcpay.store.cancreateinvoice:OTHER", "btcpay.store.canviewinvoices:S", "btcpay.store.webhooks.canmodifywebhooks:S"]
        self.assertEqual(missing_permissions(perms, "S")[0], ["btcpay.store.cancreateinvoice"])

    def test_all_stores(self):
        perms = ["btcpay.store.cancreateinvoice", "btcpay.store.canviewinvoices", "btcpay.store.webhooks.canmodifywebhooks"]
        self.assertEqual(missing_permissions(perms, "S")[0], [])

    def test_too_powerful(self):
        self.assertEqual(missing_permissions(["unrestricted"], "S"), ([], [], ["unrestricted"]))
        self.assertIn("btcpay.store.canmodifystoresettings", missing_permissions(["btcpay.store.canmodifystoresettings:S"], "S")[2])


if __name__ == "__main__":
    unittest.main()
