from pretix.multidomain import event_url

from .views import status, webhook

event_patterns = [
    event_url(r"^btcpay/webhook/$", webhook, name="webhook", require_live=False),
    event_url(r"^btcpay/status/(?P<order>[A-Z0-9]+)/(?P<secret>[A-Za-z0-9]+)/(?P<payment>[0-9]+)/$", status,
              name="status", require_live=False),
]
