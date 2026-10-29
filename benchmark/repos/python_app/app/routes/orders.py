"""Order endpoints."""

from fastapi import APIRouter, HTTPException

from app.database import get_order

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/{order_id}")
def read_order(order_id: str) -> dict:
    """Return a single order by id."""
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
