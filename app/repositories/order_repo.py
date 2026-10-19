from uuid import UUID
import psycopg


# Inserts a new order row into the orders table with initial status 'created'.
# Establishes the order record with the server-verified total_paise and associates it with the authenticated user.
def create_order(
    conn: psycopg.Connection, user_id: int, total_paise: int
) -> dict:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders (user_id, status, total_paise, razorpay_order_id)
            VALUES (%s, 'created', %s, NULL)
            RETURNING id, user_id, status, total_paise, created_at;
            """,
            (user_id, total_paise),
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
            SELECT id, user_id, status, total_paise, created_at
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
