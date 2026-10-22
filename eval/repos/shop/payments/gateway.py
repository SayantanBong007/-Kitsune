import json
import urllib.request

STRIPE_URL = "https://api.stripe.com/v1"


class StripeGateway:
    """Talks to the Stripe HTTP API."""

    def __init__(self, api_key):
        self.api_key = api_key

    def charge(self, amount_cents, currency="usd"):
        """Charge the customer's card through Stripe."""
        return _post("/charges", {"amount": amount_cents, "currency": currency}, self.api_key)


def _post(path, data, api_key):
    req = urllib.request.Request(STRIPE_URL + path, data=json.dumps(data).encode(),
                                 headers={"Authorization": f"Bearer {api_key}"})
    return urllib.request.urlopen(req)
