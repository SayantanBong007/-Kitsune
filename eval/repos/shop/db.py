"""Tiny database layer used by every model."""
import sqlite3

_conn = None


def connect(path="shop.db"):
    global _conn
    _conn = sqlite3.connect(path)
    return _conn


def insert(table, row):
    """Insert one row into a table."""
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    _conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def query(sql, *params):
    return _conn.execute(sql, params).fetchall()
