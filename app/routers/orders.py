from uuid import UUID
from fastapi import APIRouter, Depends, status

from app.core.security import get_current_user
from app.schemas.order import OrderCreate, OrderResponse
from app.services import order_service

router = APIRouter(prefix="/orders", tags=["orders"])


# Handles POST /api/orders to create a new order atomically.
# Requires user authentication, delegates validation and stock deduction to the service, and returns 201 with order details.
@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def place_order(
    order_data: OrderCreate,
    user_id=Depends(get_current_user),
):
    return order_service.create_order(user_id, order_data)


# Handles GET /api/orders/{id} to retrieve a customer's specific order.
# Requires authentication and only returns the order if it belongs to the authenticated user; otherwise responds with 404.
@router.get("/{id}", response_model=OrderResponse)
def get_order(
    id: UUID,
    user_id=Depends(get_current_user),
):
    return order_service.get_order_by_id(id, user_id)
