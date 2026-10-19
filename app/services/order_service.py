from collections import defaultdict
from uuid import UUID
from fastapi import HTTPException, status

from app.core.database import transaction
from app.repositories import order_repo, product_repo
from app.schemas.order import OrderCreate


# Creates an order atomically inside ONE single database transaction.
# Deduplicates items, decrements stock in sorted key order, computes total in paise, and saves the order.
def create_order(user_id: int, order_data: OrderCreate) -> dict:
    if not order_data.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order must contain at least one item",
        )

    # Step a: Merge duplicate product_ids by summing their quantities
    merged_quantities: dict[UUID, int] = defaultdict(int)
    for item in order_data.items:
        if item.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Quantity for product {item.product_id} must be greater than zero",
            )
        merged_quantities[item.product_id] += item.quantity

    # Sort products by ID to guarantee consistent lock ordering and eliminate deadlocks
    sorted_items = sorted(merged_quantities.items(), key=lambda x: str(x[0]))

    # Execute all checks, stock decrements, and inserts inside ONE transaction() call
    with transaction() as conn:
        processed_items = []
        total_paise = 0

        for product_id, quantity in sorted_items:
            # Step b: Atomically decrement stock and obtain catalog price
            price_paise = product_repo.decrement_product_stock(
                conn, product_id, quantity
            )

            if price_paise is None:
                # Distinguish between non-existent product and stock shortage
                product = product_repo.get_product_by_id(conn, product_id)
                if not product:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Product {product_id} not found",
                    )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insufficient stock for product {product_id}",
                )

            # Step c: Compute total_paise on the server
            total_paise += price_paise * quantity
            processed_items.append(
                {
                    "product_id": product_id,
                    "quantity": quantity,
                    "unit_price_paise": price_paise,
                }
            )

        # Step d: Insert order with status 'created' and razorpay_order_id NULL, then insert items
        order = order_repo.create_order(conn, user_id, total_paise)
        order_items = order_repo.create_order_items(
            conn, order["id"], processed_items
        )
        order["items"] = order_items
        return order


# Fetches an order and its line items, ensuring it belongs to the logged-in user.
# Returns 404 if the order does not exist or belongs to another user (never returns 403).
def get_order_by_id(order_id: UUID, user_id: int) -> dict:
    with transaction() as conn:
        order = order_repo.get_order_by_id_and_user(conn, order_id, user_id)
        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )
        return order
