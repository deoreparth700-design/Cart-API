from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class OrderItemCreate(BaseModel):
    """
    Schema for an individual item in an order placement request.
    Validates that product_id is a valid UUID and quantity is strictly greater than 0.
    """
    product_id: UUID
    quantity: int = Field(gt=0, description="Quantity to purchase, must be at least 1")


class OrderCreate(BaseModel):
    """
    Schema for creating an order.
    Validates that items is not empty. Any client-supplied amount or price fields are strictly ignored.
    """
    items: list[OrderItemCreate] = Field(min_length=1, description="List of items to order")

    model_config = ConfigDict(extra="ignore")


class OrderItemResponse(BaseModel):
    """
    Schema for an individual order item returned in order responses.
    Shows the immutable unit_price_paise captured at the time the order was created.
    """
    id: UUID
    product_id: UUID
    quantity: int
    unit_price_paise: int

    model_config = ConfigDict(from_attributes=True)


class OrderResponse(BaseModel):
    """
    Schema for the complete order response.
    Includes the server-computed total in paise and the detailed items list.
    """
    id: UUID
    user_id: int
    status: str
    total_paise: int
    items: list[OrderItemResponse]

    model_config = ConfigDict(from_attributes=True)
