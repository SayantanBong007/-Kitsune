from notifications.email import send_receipt
from payments.gateway import StripeGateway

gateway = StripeGateway(api_key="sk_test")


def process_payment(order):
    """Charge an order, persist it, and email the receipt."""
    gateway.charge(order.total())
    order.save()
    send_receipt(order)


def refund_payment(order):
    gateway.charge(-order.total())
