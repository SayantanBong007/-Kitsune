from flask import Flask, request

from auth.session import login
from inventory.stock import reserve_stock
from models.order import Order
from models.user import User
from payments.service import process_payment

app = Flask(__name__)


@app.route("/checkout", methods=["POST"])
def checkout():
    """Checkout: reserve stock, take payment, confirm the order."""
    order = Order(request.user, request.json["items"])
    reserve_stock(order)
    process_payment(order)
    return {"status": "ok", "total": order.total()}


@app.route("/users", methods=["POST"])
def create_user():
    user = User(**request.json)
    user.save()
    return {"name": user.full_name()}


@app.route("/login", methods=["POST"])
def login_route():
    return {"token": login(request.json["email"], request.json["password"])}
