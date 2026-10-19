# Real database tests for Razorpay webhook idempotency, retry flows, amount verification, and gateway failure rollbacks.
# Verifies atomic payment transitions and stock consistency on isolated Neon PostgreSQL database.

import json
import uuid
import psycopg
from psycopg.rows import dict_row


# Inserts a test order into the test database to receive incoming webhook events.
def insert_test_order(conn_url: str, user_id: int, total_paise: int, razorpay_order_id: str, status: str = "created") -> dict:
    with psycopg.connect(conn_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (user_id, status, total_paise, razorpay_order_id, idempotency_key)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, user_id, status, total_paise, razorpay_order_id;
                """,
                (user_id, status, total_paise, razorpay_order_id, f"test-key-{uuid.uuid4().hex[:8]}"),
            )
            return cur.fetchone()


# Tests that receiving the same payment.captured event ID twice marks the order paid once and replies duplicate_event.
def test_duplicate_payment_captured_event_id(client, sign_webhook, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = insert_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    event_id = f"evt_dup_{uuid.uuid4().hex[:8]}"
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    signature = sign_webhook(raw_bytes)
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": event_id,
    }

    # First event delivery: processes and marks order paid
    res1 = client.post("/api/webhooks/razorpay", content=raw_bytes, headers=headers)
    assert res1.status_code == 200
    assert res1.json()["status"] == "processed"
    assert get_order_row(order_id)["status"] == "paid"

    # Second event delivery (same event ID): safely ignored as duplicate_event
    res2 = client.post("/api/webhooks/razorpay", content=raw_bytes, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == "ignored"
    assert res2.json()["reason"] == "duplicate_event"
    assert get_order_row(order_id)["status"] == "paid"


# Tests that receiving order.paid after payment.captured with a different event ID returns already_paid with no changes.
def test_payment_captured_then_order_paid_with_different_event_id(client, sign_webhook, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = insert_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    # 1. First event: payment.captured
    payload1 = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw1 = json.dumps(payload1).encode("utf-8")
    res1 = client.post(
        "/api/webhooks/razorpay",
        content=raw1,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw1),
            "X-Razorpay-Event-Id": f"evt_cap_{uuid.uuid4().hex[:8]}",
        },
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "processed"
    assert get_order_row(order_id)["status"] == "paid"

    # 2. Second event: order.paid with different event ID
    payload2 = {
        "event": "order.paid",
        "payload": {
            "order": {
                "entity": {
                    "id": rzp_order_id,
                    "amount": 10000,
                }
            }
        },
    }
    raw2 = json.dumps(payload2).encode("utf-8")
    res2 = client.post(
        "/api/webhooks/razorpay",
        content=raw2,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw2),
            "X-Razorpay-Event-Id": f"evt_ord_{uuid.uuid4().hex[:8]}",
        },
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "already_paid"
    assert get_order_row(order_id)["status"] == "paid"


# Tests that receiving payment.failed leaves the order in 'created' status and leaves stock completely untouched.
def test_payment_failed_leaves_order_created_and_stock_untouched(client, products, sign_webhook, get_order_row, get_stock):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = insert_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw_bytes),
            "X-Razorpay-Event-Id": f"evt_fail_{uuid.uuid4().hex[:8]}",
        },
    )

    assert res.status_code == 200
    assert res.json()["status"] == "processed"
    assert get_order_row(order_id)["status"] == "created", "Order must stay in 'created' status"
    assert get_stock(pen["id"]) == initial_stock, "Stock must remain unchanged"


# Tests the customer retry flow: payment.failed followed by payment.captured ends in 'paid' status with stock unchanged.
def test_failed_then_captured_retry_flow(client, products, sign_webhook, get_order_row, get_stock):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = insert_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    # Step 1: payment.failed event
    payload_fail = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:8]}", "order_id": rzp_order_id}}},
    }
    raw_fail = json.dumps(payload_fail).encode("utf-8")
    res1 = client.post(
        "/api/webhooks/razorpay",
        content=raw_fail,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw_fail),
            "X-Razorpay-Event-Id": f"evt_fail_{uuid.uuid4().hex[:8]}",
        },
    )
    assert res1.status_code == 200
    assert get_order_row(order_id)["status"] == "created"
    assert get_stock(pen["id"]) == initial_stock

    # Step 2: Customer retries payment successfully -> payment.captured
    payload_cap = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_cap = json.dumps(payload_cap).encode("utf-8")
    res2 = client.post(
        "/api/webhooks/razorpay",
        content=raw_cap,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw_cap),
            "X-Razorpay-Event-Id": f"evt_cap_{uuid.uuid4().hex[:8]}",
        },
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "processed"
    assert get_order_row(order_id)["status"] == "paid"
    assert get_stock(pen["id"]) == initial_stock


# Tests that a payment.captured webhook with the wrong amount returns amount_mismatch and leaves the order in 'created'.
def test_payment_captured_with_wrong_amount(client, sign_webhook, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = insert_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id)
    order_id = order["id"]

    # Webhook claims 5000 paise paid for an order requiring 10000 paise
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "amount": 5000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw_bytes),
            "X-Razorpay-Event-Id": f"evt_mismatch_{uuid.uuid4().hex[:8]}",
        },
    )

    assert res.status_code == 200
    assert res.json()["status"] == "amount_mismatch"
    assert get_order_row(order_id)["status"] == "created", "Order must stay in 'created' status"


# Tests that an order already in 'failed' status receiving payment.captured stays 'failed' and replies with 'ignored'.
def test_order_in_failed_status_receives_payment_captured(client, sign_webhook, get_order_row):
    from tests.conftest import test_db_url
    rzp_order_id = f"order_{uuid.uuid4().hex[:14]}"
    order = insert_test_order(test_db_url, user_id=1, total_paise=10000, razorpay_order_id=rzp_order_id, status="failed")
    order_id = order["id"]

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "amount": 10000,
                    "order_id": rzp_order_id,
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw_bytes),
            "X-Razorpay-Event-Id": f"evt_fail_cap_{uuid.uuid4().hex[:8]}",
        },
    )

    assert res.status_code == 200
    assert res.json()["status"] != "processed"
    assert res.json()["status"] == "ignored"
    assert res.json()["reason"] == "order_status_failed"
    assert get_order_row(order_id)["status"] == "failed"


# Tests that webhooks for an unknown razorpay_order_id return 200 ignored without raising exceptions or modifying data.
def test_unknown_razorpay_order_id_ignored_safely(client, sign_webhook):
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "amount": 10000,
                    "order_id": "order_non_existent_99999",
                }
            }
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/api/webhooks/razorpay",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sign_webhook(raw_bytes),
            "X-Razorpay-Event-Id": f"evt_unknown_{uuid.uuid4().hex[:8]}",
        },
    )

    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert res.json()["reason"] == "order_not_found"


# Tests that when the Razorpay SDK fails during order creation, the API returns 502, marks order failed, and restores stock.
def test_razorpay_sdk_failure_marks_order_failed_and_restores_stock(client, products, user_tokens, get_stock, get_order_row, monkeypatch):
    from app.services import razorpay_service
    user = user_tokens["user1"]
    pen = products["pen"]
    initial_stock = get_stock(pen["id"])

    # Simulate Razorpay Gateway Failure
    def failing_razorpay_call(total_paise: int, receipt: str):
        raise RuntimeError("Razorpay Gateway timeout connection error")

    monkeypatch.setattr(razorpay_service, "create_razorpay_order", failing_razorpay_call)

    payload = {"items": [{"product_id": str(pen["id"]), "quantity": 2}]}
    headers = {**user["headers"], "Idempotency-Key": f"key-rzp-fail-{uuid.uuid4().hex[:8]}"}

    response = client.post("/api/orders", json=payload, headers=headers)

    assert response.status_code == 502
    assert "failed to create razorpay payment order" in response.json()["detail"].lower()

    # Verify inventory was restored exactly once back to initial stock
    assert get_stock(pen["id"]) == initial_stock

    # Verify order was marked failed in the database
    from tests.conftest import test_db_url
    with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE user_id = %s;", (user["id"],))
            db_order = cur.fetchone()
            assert db_order is not None
            assert db_order["status"] == "failed"
