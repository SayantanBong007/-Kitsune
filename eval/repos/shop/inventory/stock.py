import db


def decrement(sku, qty):
    """Remove qty units of a SKU from stock."""
    (on_hand,) = db.query("SELECT qty FROM stock WHERE sku = ?", sku)[0]
    if on_hand < qty:
        raise ValueError(f"insufficient stock for {sku}")
    db.insert("stock_moves", {"sku": sku, "delta": -qty})


def reserve_stock(order):
    """Reserve inventory for every item in an order."""
    for item in order.items:
        decrement(item.sku, item.qty)
