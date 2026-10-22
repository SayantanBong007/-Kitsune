import smtplib


def render_template(name, ctx):
    with open(f"templates/{name}.txt") as fh:
        return fh.read().format(**ctx)


def send_email(to, subject, body):
    """Deliver an email over SMTP."""
    with smtplib.SMTP("localhost") as smtp:
        smtp.sendmail("shop@example.com", [to], f"Subject: {subject}\n\n{body}")


def send_receipt(order):
    """Email the customer a receipt for their order."""
    body = render_template("receipt", {"total": order.total()})
    send_email(order.user.email, "Your receipt", body)
