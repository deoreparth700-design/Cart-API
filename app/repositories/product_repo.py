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
