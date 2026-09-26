"""
PostgreSQL advisory locks for what has to happen one at a time: the work on an order's BTCPay payments and the
registration of the webhook.

Not a lease in the cache: that can run out while its owner still works, and it ends before pretix commits the
transaction it ran in, so the next worker would act on a state that is not committed yet. Not a row lock either:
pretix takes the order row and its quota locks in different orders on different paths, and waiting on a row here
could close a cycle.

Inside a transaction the lock lasts until that transaction commits or rolls back; outside one it lasts for the
block. It is only ever tried, never waited for inside PostgreSQL, so it cannot take part in a deadlock: after
``wait`` seconds of trying, ``busy`` is raised. Keys use the two-number form, which PostgreSQL keeps apart from the
one-number keys of pretix' own locks. Session locks need a connection of their own (no pgbouncer in transaction mode).
On other databases (SQLite in development, which writes one at a time anyway) it does nothing.
"""
import logging
import threading
import time
from contextlib import contextmanager

from django.db import DatabaseError, connection

logger = logging.getLogger(__name__)

ORDER = 0x42545001  # "BTP" and a kind, as the first number of the key
WEBHOOK = 0x42545002


class Busy(Exception):
    """Someone else holds the lock."""


_held = threading.local()


@contextmanager
def advisory_lock(space: int, key: int, wait: float = 0, busy: type[Exception] = Busy):
    """Hold the lock (``space``, ``key``) for the block; reentrant within a thread."""
    held = getattr(_held, "keys", None)
    if held is None:
        held = _held.keys = set()
    ident = (space, int(key) % 2 ** 31)
    if connection.vendor != "postgresql" or ident in held:
        yield
        return
    in_transaction = connection.in_atomic_block
    function = "pg_try_advisory_xact_lock" if in_transaction else "pg_try_advisory_lock"
    until = time.monotonic() + wait
    with connection.cursor() as cursor:
        while True:
            cursor.execute(f"SELECT {function}(%s, %s)", list(ident))
            if cursor.fetchone()[0]:
                break
            if time.monotonic() >= until:
                raise busy()
            time.sleep(0.2)
    held.add(ident)
    try:
        yield
    finally:
        held.discard(ident)
        if not in_transaction:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s, %s)", list(ident))
            except DatabaseError:
                # A broken connection gives its locks up when it closes.
                logger.warning("BTCPay: could not release lock %s", ident, exc_info=True)
