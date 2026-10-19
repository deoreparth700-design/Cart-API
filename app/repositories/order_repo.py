from uuid import UUID
import psycopg


# Inserts a new order row into the orders table with initial status 'created' and the provided idempotency key.
# Establishes the order record with the server-verified total_paise, associates it with the authenticated user, and stores the idempotency key for duplicate prevention.
def create_order(
    conn: psycopg.Connection, user_id: int, total_paise: int, idempotency_key: str
) -> dict:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders (user_id, status, total_paise, razorpay_order_id, idempotency_key)
            VALUES (%s, 'created', %s, NULL, %s)
            RETURNING id, user_id, status, total_paise, created_at;
            """,
            (user_id, total_paise, idempotency_key),
        )
        return cursor.fetchone()


# Inserts purchased item rows into order_items with prices frozen at order time.
# Storing unit_price_paise directly ensures subsequent catalog price updates never alter past receipts.
def create_order_items(
    conn: psycopg.Connection, order_id: UUID, items: list[dict]
) -> list[dict]:
    created_items = []
    with conn.cursor() as cursor:
        for item in items:
            cursor.execute(
                """
                INSERT INTO order_items (order_id, product_id, quantity, unit_price_paise)
                VALUES (%s, %s, %s, %s)
                RETURNING id, product_id, quantity, unit_price_paise;
                """,
                (
                    order_id,
                    item["product_id"],
                    item["quantity"],
                    item["unit_price_paise"],
                ),
            )
            created_items.append(cursor.fetchone())
    return created_items


# Retrieves an order and its associated line items only if it belongs to the given user.
# Guarantees user isolation so customers can never view another user's order details.
def get_order_by_id_and_user(
    conn: psycopg.Connection, order_id: UUID, user_id: int
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, user_id, status, total_paise, razorpay_order_id, created_at
            FROM orders
            WHERE id = %s AND user_id = %s;
            """,
            (order_id, user_id),
        )
        order = cursor.fetchone()
        if not order:
            return None

        cursor.execute(
            """
            SELECT id, product_id, quantity, unit_price_paise
            FROM order_items
            WHERE order_id = %s;
            """,
            (order_id,),
        )
        order["items"] = cursor.fetchall()
        return order


# Looks up an order and its line items using the authenticated user's ID and an idempotency key.
# Returns the complete existing order record if one was previously placed with this key, or None if no match is found.
def get_order_by_idempotency_key(
    conn: psycopg.Connection, user_id: int, key: str
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, user_id, status, total_paise, razorpay_order_id, created_at
            FROM orders
            WHERE user_id = %s AND idempotency_key = %s;
            """,
            (user_id, key),
        )
        order = cursor.fetchone()
        if not order:
            return None

        cursor.execute(
            """
            SELECT id, product_id, quantity, unit_price_paise
            FROM order_items
            WHERE order_id = %s;
            """,
            (order["id"],),
        )
        order["items"] = cursor.fetchall()
        return order


# Updates an order with its external Razorpay order identifier after successful creation with the gateway.
def set_razorpay_order_id(
    conn: psycopg.Connection, order_id: UUID, razorpay_order_id: str
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders
            SET razorpay_order_id = %s
            WHERE id = %s;
            """,
            (razorpay_order_id, order_id),
        )


# Marks an order as failed when payment order generation encounters an error.
def mark_order_failed(
    conn: psycopg.Connection, order_id: UUID
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders
            SET status = 'failed'
            WHERE id = %s;
            """,
            (order_id,),
        )


# Retrieves an order and its line items using the Razorpay order ID.
# Used by webhooks to locate the internal order corresponding to incoming payment events.
def get_order_by_razorpay_order_id(
    conn: psycopg.Connection, razorpay_order_id: str
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, user_id, status, total_paise, razorpay_order_id, razorpay_payment_id, created_at
            FROM orders
            WHERE razorpay_order_id = %s;
            """,
            (razorpay_order_id,),
        )
        order = cursor.fetchone()
        if not order:
            return None

        cursor.execute(
            """
            SELECT id, product_id, quantity, unit_price_paise
            FROM order_items
            WHERE order_id = %s;
            """,
            (order["id"],),
        )
        order["items"] = cursor.fetchall()
        return order


# Updates an order status to 'paid' and records the Razorpay payment ID only if currently in 'created' status.
# Transitions the order upon successful payment capture confirmed by webhook.
def mark_order_paid(
    conn: psycopg.Connection, razorpay_order_id: str, razorpay_payment_id: str
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders
            SET status = 'paid', razorpay_payment_id = %s
            WHERE razorpay_order_id = %s AND status = 'created'
            RETURNING id, user_id, status, total_paise, razorpay_order_id, razorpay_payment_id;
            """,
            (razorpay_payment_id, razorpay_order_id),
        )
        return cursor.fetchone()




