"""Billing calculations."""

from app.orders import OrderLine


def calculate_total(lines):
    """Sum the line items of an order."""
    return sum(line.quantity * line.unit_cents for line in lines)


def describe_line(line: OrderLine) -> str:
    """Return a human readable description of one line item."""
    return f"{line.sku} x{line.quantity}"
