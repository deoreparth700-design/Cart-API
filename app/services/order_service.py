from collections import defaultdict
from uuid import UUID
from fastapi import HTTPException, status
from psycopg.errors import UniqueViolation

from app.core.config import settings
from app.core.database import transaction
from app.repositories import order_repo, product_repo
from app.schemas.order import OrderCreate
from app.services import razorpay_service


# Creates an order idempotently and generates a corresponding Razorpay payment order.
# Decrements stock inside an initial database transaction, creates the payment order with Razorpay,
# and rolls back stock while marking the order as failed if the payment gateway call fails.
def create_order(
    user_id: int, order_data: OrderCreate, idempotency_key: str
) -> tuple[dict, bool]:
    # Step 1: Look up existing order by (user_id, idempotency_key) BEFORE touching stock
    with transaction() as conn:
        existing_order = order_repo.get_order_by_idempotency_key(
            conn, user_id, idempotency_key
        )
        if existing_order:
            existing_order["razorpay_key_id"] = settings.RAZORPAY_KEY_ID
            existing_order["RAZORPAY_KEY_ID"] = settings.RAZORPAY_KEY_ID
            return existing_order, False

    if not order_data.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order must contain at least one item",
        )

    # Step 2: Merge duplicate product_ids by summing their quantities
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

    # Step 3: Run Phase 4 database transaction with race protection
    try:
        with transaction() as conn:
            processed_items = []
            total_paise = 0

            for product_id, quantity in sorted_items:
                # Atomically decrement stock and obtain catalog price
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

                # Compute total_paise on the server
                total_paise += price_paise * quantity
                processed_items.append(
                    {
                        "product_id": product_id,
                        "quantity": quantity,
                        "unit_price_paise": price_paise,
                    }
                )

            # Insert order with status 'created', idempotency_key, and insert line items
            order = order_repo.create_order(
                conn, user_id, total_paise, idempotency_key
            )
            order_items = order_repo.create_order_items(
                conn, order["id"], processed_items
            )
            order["items"] = order_items

    except UniqueViolation as err:
        # Race protection: another concurrent request inserted an order with the same (user_id, idempotency_key).
        # The database transaction automatically rolled back (including the stock decrement).
        # Only treat it as an idempotency duplicate if the violated constraint is 'unique_user_idempotency'.
        if getattr(err, "diag", None) and err.diag.constraint_name == "unique_user_idempotency":
            with transaction() as conn:
                existing_order = order_repo.get_order_by_idempotency_key(
                    conn, user_id, idempotency_key
                )
                if existing_order:
                    existing_order["razorpay_key_id"] = settings.RAZORPAY_KEY_ID
                    existing_order["RAZORPAY_KEY_ID"] = settings.RAZORPAY_KEY_ID
                    return existing_order, False
        raise

    # Step 4: After the Phase 4 transaction has committed, create the Razorpay order
    try:
        razorpay_order_id = razorpay_service.create_razorpay_order(
            total_paise=total_paise,
            receipt=str(order["id"]),
        )
    except Exception as exc:
        # If the Razorpay call fails: run a second transaction that sets the order to 'failed'
        # and gives the stock back for every item, then return HTTP 502 Bad Gateway.
        with transaction() as conn:
            order_repo.mark_order_failed(conn, order["id"])
            for item in processed_items:
                product_repo.increment_product_stock(
                    conn, item["product_id"], item["quantity"]
                )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Razorpay payment order: {str(exc)}",
        )

    # Save the returned razorpay_order_id on our order in a second transaction
    with transaction() as conn:
        order_repo.set_razorpay_order_id(conn, order["id"], razorpay_order_id)

    order["razorpay_order_id"] = razorpay_order_id
    order["razorpay_key_id"] = settings.RAZORPAY_KEY_ID
    order["RAZORPAY_KEY_ID"] = settings.RAZORPAY_KEY_ID
    return order, True


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
        order["razorpay_key_id"] = settings.RAZORPAY_KEY_ID
        order["RAZORPAY_KEY_ID"] = settings.RAZORPAY_KEY_ID
        return order
