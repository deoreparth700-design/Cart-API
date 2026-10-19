from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.core.security import get_current_user
from app.schemas.order import OrderCreate, OrderResponse
from app.services import order_service

router = APIRouter(prefix="/orders", tags=["orders"])


# Handles POST /api/orders to place an order idempotently using the Idempotency-Key header.
# Rejects missing, empty, or oversized keys with HTTP 400, returns 201 for new orders, and returns 200 for repeated requests.
@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def place_order(
    response: Response,
    order_data: OrderCreate,
    user_id=Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required and cannot be empty",
        )

    if len(idempotency_key) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header cannot exceed 255 characters",
        )

    order, is_created = order_service.create_order(
        user_id, order_data, idempotency_key
    )

    if not is_created:
        response.status_code = status.HTTP_200_OK

    return order


# Handles GET /api/orders/{id} to retrieve a customer's specific order.
# Requires authentication and only returns the order if it belongs to the authenticated user; otherwise responds with 404.
@router.get("/{id}", response_model=OrderResponse)
def get_order(
    id: UUID,
    user_id=Depends(get_current_user),
):
    return order_service.get_order_by_id(id, user_id)
