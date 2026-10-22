import db
from models.base import BaseModel


class User(BaseModel):
    table = "users"

    def __init__(self, email, first, last):
        self.email, self.first, self.last = email, first, last

    def full_name(self):
        return f"{self.first} {self.last}"

    def save(self):
        db.insert(self.table, self.to_dict())
