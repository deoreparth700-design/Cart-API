import psycopg


# Records a webhook event ID in the database to prevent duplicate processing.
# Uses ON CONFLICT DO NOTHING on event_id; returns True if the record was inserted or False if it already existed.
def record_webhook_event(
    conn: psycopg.Connection, event_id: str, event_type: str
) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO webhook_events (event_id, event_type)
            VALUES (%s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id;
            """,
            (event_id, event_type),
        )
        return cursor.fetchone() is not None


# Retrieves a recorded webhook event by its event ID to inspect when or if it was previously processed.
def get_webhook_event(
    conn: psycopg.Connection, event_id: str
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_id, event_type, received_at
            FROM webhook_events
            WHERE event_id = %s;
            """,
            (event_id,),
        )
        return cursor.fetchone()
