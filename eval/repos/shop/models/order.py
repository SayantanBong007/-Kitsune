import db
from models.base import BaseModel


class Order(BaseModel):
    """A customer order with line items."""

    table = "orders"

    def __init__(self, user, items):
        self.user, self.items = user, items

    def total(self):
        return sum(item.price * item.qty for item in self.items)

    def validate(self):
        if not self.items:
            raise ValueError("empty order")

    def save(self):
        self.validate()
        db.insert(self.table, self.to_dict())
