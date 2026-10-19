# Real database tests verifying Razorpay webhook signature validation.
# Ensures forged, missing, or tampered webhook requests are rejected with HTTP 401 without touching the database.

import json
import uuid
import psycopg
from psycopg.rows import dict_row


# Inserts a test order into the test database to receive incoming webhook events.
def create_test_order(conn_url: str, user_id: int, total_paise: int, razorpay_order_id: str) -> dict:
    with psycopg.connect(conn_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (user_id, status, total_paise, razorpay_order_id, idempotency_key)
                VALUES (%s, 'created', %s, %s, %s)
                RETURNING id, user_id, status, total_paise, razorpay_order_id;
                """,
                (user_id, total_paise, razorpay_order_id, f"test-key-{uuid.uuid4().hex[:8]}"),
            )
            return cur.fetchone()


# Tests that a correctly signed webhook request is accepted and updates the order in the test database.
def test_valid_signature_accepted(client, sign_webhook, count_webhook_events, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = create_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:10]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    signature = sign_webhook(raw_bytes)

    response = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:10]}",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert count_webhook_events() == 1
    updated_order = get_order_row(order_id)
    assert updated_order["status"] == "paid"


# Tests that modifying the request body after generating the signature results in HTTP 401 with no database changes.
def test_tampered_body_rejected_with_401(client, sign_webhook, count_webhook_events, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = create_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:10]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    signature = sign_webhook(raw_bytes)

    # Tamper with the payload body bytes after signing
    tampered_bytes = raw_bytes + b" "

    response = client.post(
        "/api/webhooks/razorpay",
        content=tampered_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:10]}",
        },
    )

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()
    assert count_webhook_events() == 0, "Webhook event must not be recorded when signature check fails"
    db_order = get_order_row(order_id)
    assert db_order["status"] == "created", "Order status must remain 'created'"


# Tests that omitting the X-Razorpay-Signature header results in HTTP 401 with no database changes.
def test_missing_signature_header_rejected_with_401(client, count_webhook_events, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = create_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:10]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:10]}",
        },
    )

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()
    assert count_webhook_events() == 0, "Webhook event must not be recorded when signature is missing"
    db_order = get_order_row(order_id)
    assert db_order["status"] == "created", "Order status must remain 'created'"


# Tests that sending an empty X-Razorpay-Signature header results in HTTP 401 with no database changes.
def test_empty_signature_header_rejected_with_401(client, count_webhook_events, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = create_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:10]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": "",
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:10]}",
        },
    )

    assert response.status_code == 401
    assert "signature" in response.json()["detail"].lower()
    assert count_webhook_events() == 0, "Webhook event must not be recorded when signature is empty"
    db_order = get_order_row(order_id)
    assert db_order["status"] == "created", "Order status must remain 'created'"
