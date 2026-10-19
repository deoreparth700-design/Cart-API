import hashlib
import hmac
import json
import uuid
from contextlib import contextmanager
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import webhook_service


DUMMY_WEBHOOK_SECRET = "test_webhook_secret_key_12345"


@pytest.fixture
def client():
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


def generate_signature(payload_bytes: bytes, secret: str = DUMMY_WEBHOOK_SECRET) -> str:
    """Generates a valid HMAC-SHA256 signature for test payloads."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def make_payment_captured_payload(
    razorpay_order_id: str,
    razorpay_payment_id: str = "pay_test123",
    amount: int = 15000,
) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": razorpay_payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                    "order_id": razorpay_order_id,
                }
            }
        },
        "created_at": 1600000000,
    }


def make_payment_failed_payload(
    razorpay_order_id: str,
    razorpay_payment_id: str = "pay_fail123",
    amount: int = 15000,
) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": razorpay_payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": razorpay_order_id,
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment failed by customer",
                }
            }
        },
        "created_at": 1600000000,
    }


# Case 1 & 3: Valid signature is accepted using raw HTTP body and HMAC-SHA256
def test_valid_signature_is_accepted(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    payload_dict = make_payment_captured_payload("order_valid_123")
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = generate_signature(payload_bytes)

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": uuid.uuid4(), "status": "created"},
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", lambda conn, r_id, p_id: None)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_valid_1",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert response.json()["event"] == "payment.captured"


# Case 2: Missing, malformed or invalid signature is rejected without modifying database
def test_invalid_or_missing_signature_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    payload_bytes = json.dumps(make_payment_captured_payload("order_123")).encode("utf-8")
    valid_sig = generate_signature(payload_bytes)

    # Database modification flag to prove no DB interaction occurred
    db_touched = False
    @contextmanager
    def guard_tx():
        nonlocal db_touched
        db_touched = True
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", guard_tx)

    test_cases = [
        {},                                         # Missing header
        {"X-Razorpay-Signature": ""},               # Empty header
        {"X-Razorpay-Signature": "invalid_sig_abc"},# Invalid signature
        {"X-Razorpay-Signature": valid_sig[:-2]},   # Truncated/malformed signature
    ]

    for headers in test_cases:
        db_touched = False
        response = client.post(
            "/api/webhooks/razorpay",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Event-Id": "evt_test",
                **headers,
            },
        )
        assert response.status_code == 400
        assert "signature" in response.json()["detail"].lower()
        assert db_touched is False, "Database must NOT be accessed when signature check fails"


# Case 4: A valid payment.captured event updates correct order to paid and records payment ID
def test_payment_captured_updates_order_to_paid(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_pay_cap"
    mock_rzp_pay_id = "pay_captured_999"

    payload_bytes = json.dumps(
        make_payment_captured_payload(mock_rzp_order_id, mock_rzp_pay_id)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    marked_paid_args = []
    def mock_mark_paid(conn, rzp_order_id, rzp_pay_id):
        marked_paid_args.append((rzp_order_id, rzp_pay_id))

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": mock_order_id, "status": "created", "items": []},
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", mock_mark_paid)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_captured_100",
        },
    )

    assert response.status_code == 200
    assert marked_paid_args == [(mock_rzp_order_id, mock_rzp_pay_id)]
    assert response.json()["order_id"] == str(mock_order_id)


# Case 5: A valid payment.failed event updates order to failed and restores stock
def test_payment_failed_updates_order_and_restores_stock(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_prod_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_pay_fail"
    mock_rzp_pay_id = "pay_failed_456"

    payload_bytes = json.dumps(
        make_payment_failed_payload(mock_rzp_order_id, mock_rzp_pay_id)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    failed_args = []
    def mock_mark_failed(conn, rzp_order_id, rzp_pay_id):
        failed_args.append((rzp_order_id, rzp_pay_id))

    restored_items = []
    def mock_increment_stock(conn, prod_id, qty):
        restored_items.append((prod_id, qty))

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {
            "id": mock_order_id,
            "status": "created",
            "items": [{"product_id": mock_prod_id, "quantity": 3}],
        },
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_payment_failed", mock_mark_failed)
    monkeypatch.setattr(webhook_service.product_repo, "increment_product_stock", mock_increment_stock)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_failed_200",
        },
    )

    assert response.status_code == 200
    assert failed_args == [(mock_rzp_order_id, mock_rzp_pay_id)]
    assert restored_items == [(mock_prod_id, 3)]
    assert response.json()["status"] == "processed"
    assert response.json()["event"] == "payment.failed"


# Case 6: Unknown or unmatched orders are handled safely without crashing
def test_unknown_order_handled_safely(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    payload_bytes = json.dumps(
        make_payment_captured_payload("order_non_existent_999")
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    # Order lookup returns None (unmatched order)
    monkeypatch.setattr(webhook_service.order_repo, "get_order_by_razorpay_order_id", lambda conn, r_id: None)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_unknown_order",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "order_not_found"


# Case 7: Duplicate deliveries carrying the same x-razorpay-event-id do not process twice
def test_duplicate_event_id_ignored_safely(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    payload_bytes = json.dumps(
        make_payment_captured_payload("order_duplicate_test")
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    order_updated = False
    def unexpected_order_update(*args, **kwargs):
        nonlocal order_updated
        order_updated = True

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    # Return False to simulate event already recorded in webhook_events
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: False)
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", unexpected_order_update)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_duplicate_555",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "duplicate_event"
    assert order_updated is False, "Duplicate event must not update the order"


# Case 8 & 10: Atomic transaction rollback when database failure occurs
def test_database_failure_rolls_back_atomically(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    payload_bytes = json.dumps(
        make_payment_captured_payload("order_rollback_test")
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    rolled_back = False

    class FakeConn:
        pass

    @contextmanager
    def failing_tx():
        nonlocal rolled_back
        try:
            yield FakeConn()
        except Exception:
            rolled_back = True
            raise

    monkeypatch.setattr(webhook_service, "transaction", failing_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": uuid.uuid4(), "status": "created"},
    )

    def failing_mark_paid(conn, r_id, p_id):
        raise RuntimeError("Simulated database failure during order update")

    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", failing_mark_paid)

    with pytest.raises(RuntimeError, match="Simulated database failure"):
        client.post(
            "/api/webhooks/razorpay",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": sig,
                "X-Razorpay-Event-Id": "evt_rollback_1",
            },
        )

    assert rolled_back is True, "Database failure must trigger transaction rollback"


# Case 9: Missing event IDs and malformed payloads are rejected with 400
def test_missing_event_id_and_malformed_payload(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    # 1. Missing event ID header and body
    payload_no_eid = json.dumps(make_payment_captured_payload("order_123")).encode("utf-8")
    sig1 = generate_signature(payload_no_eid)

    res1 = client.post(
        "/api/webhooks/razorpay",
        content=payload_no_eid,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig1,
            # No X-Razorpay-Event-Id header
        },
    )
    assert res1.status_code == 400
    assert "event id" in res1.json()["detail"].lower()

    # 2. Malformed non-JSON payload
    malformed_body = b"not a json string at all {{"
    sig2 = generate_signature(malformed_body)

    res2 = client.post(
        "/api/webhooks/razorpay",
        content=malformed_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig2,
            "X-Razorpay-Event-Id": "evt_malformed",
        },
    )
    assert res2.status_code == 400
    assert "malformed" in res2.json()["detail"].lower()


# Case 11: An already-paid order cannot accidentally be downgraded by duplicate or delayed failure event
def test_already_paid_order_cannot_be_downgraded_by_failure_event(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_already_paid_123"

    payload_bytes = json.dumps(
        make_payment_failed_payload(mock_rzp_order_id, "pay_failed_delayed")
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    status_changed_to_failed = False
    def unexpected_fail_update(*args, **kwargs):
        nonlocal status_changed_to_failed
        status_changed_to_failed = True

    stock_restored = False
    def unexpected_stock_restore(*args, **kwargs):
        nonlocal stock_restored
        stock_restored = True

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    # Order is already 'paid'
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {
            "id": mock_order_id,
            "status": "paid",
            "items": [{"product_id": uuid.uuid4(), "quantity": 2}],
        },
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_payment_failed", unexpected_fail_update)
    monkeypatch.setattr(webhook_service.product_repo, "increment_product_stock", unexpected_stock_restore)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_delayed_fail",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "order_already_paid"
    assert status_changed_to_failed is False, "Already-paid order must NOT be marked failed"
    assert stock_restored is False, "Stock must NOT be restored for already-paid order"
