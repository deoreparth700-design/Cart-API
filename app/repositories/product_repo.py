from uuid import UUID
import psycopg


# Retrieves all product records from the database sorted alphabetically by name.
# This repository function isolates raw SQL execution from route handlers and service layers.
def list_products(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, price_paise, stock
            FROM products
            ORDER BY name ASC;
            """
        )
        return cursor.fetchall()


# Fetches a single product by its unique identifier.
# Used to check whether a product exists in the catalog before reporting insufficient stock.
def get_product_by_id(conn: psycopg.Connection, product_id: UUID) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, price_paise, stock
            FROM products
            WHERE id = %s;
            """,
            (product_id,),
        )
        return cursor.fetchone()


# Decrements stock for a product atomically if sufficient stock is available.
# Uses 'AND stock >= %s' to prevent negative stock and returns the official price_paise from the database.
def decrement_product_stock(
    conn: psycopg.Connection, product_id: UUID, quantity: int
) -> int | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE products
            SET stock = stock - %s
            WHERE id = %s AND stock >= %s
            RETURNING price_paise;
            """,
            (quantity, product_id, quantity),
        )
        row = cursor.fetchone()
        return row["price_paise"] if row else None
