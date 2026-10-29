"""Order search endpoint."""

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.database import search_orders

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/search")
def search(status: str, user_id: str = Depends(get_current_user)) -> list[dict]:
    """List the caller's orders in the given status."""
    return search_orders(status)
