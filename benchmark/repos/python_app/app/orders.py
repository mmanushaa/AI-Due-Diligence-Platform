"""Order domain model."""

from dataclasses import dataclass

from app.billing import calculate_total


@dataclass
class OrderLine:
    """One line item on an order."""

    sku: str
    quantity: int
    unit_cents: int


def summarize(lines: list[OrderLine]) -> dict:
    """Return the total for the given lines."""
    return {"total_cents": calculate_total(lines)}


def order_from_row(row: dict) -> dict:
    """Shape a database row for the API response."""
    return {"id": row["id"], "status": row["status"], "total_cents": row["total_cents"]}
