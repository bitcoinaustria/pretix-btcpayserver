"""Isolated test runner inside the official pretix image; SQLite or disposable CI PostgreSQL."""
import os

from pretix.settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
if os.environ.get('PRETIX_REVIEW_POSTGRES') == '1':
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "review_fixture",
                             "USER": "review_fixture",
                             "HOST": "127.0.0.1", "PORT": "5432"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
if not any(app == "pretix_btcpay" or app.startswith("pretix_btcpay.") for app in INSTALLED_APPS):  # noqa: F405
    INSTALLED_APPS = [*INSTALLED_APPS, "pretix_btcpay"]  # noqa: F405
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


class ModelSchemaOnly(dict):
    # This feature has no migrations. Build the current model schema instead of replaying
    # pretix's full migration history for every small pricing test (also under emulation).
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return None


MIGRATION_MODULES = ModelSchemaOnly()
