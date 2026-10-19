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


def make_order_paid_payload(
    razorpay_order_id: str,
    razorpay_payment_id: str = "pay_test123",
    amount: int = 15000,
) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "order.paid",
        "contains": ["order", "payment"],
        "payload": {
            "order": {
                "entity": {
                    "id": razorpay_order_id,
                    "entity": "order",
                    "amount": amount,
                    "currency": "INR",
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": razorpay_payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                    "order_id": razorpay_order_id,
                }
            },
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

    payload_dict = make_payment_captured_payload("order_valid_123", amount=15000)
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
        lambda conn, r_id: {"id": uuid.uuid4(), "status": "created", "total_paise": 15000},
    )
    monkeypatch.setattr(
        webhook_service.order_repo,
        "mark_order_paid",
        lambda conn, r_id, p_id: {"id": uuid.uuid4(), "status": "paid"},
    )


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
        assert response.status_code == 401
        assert "signature" in response.json()["detail"].lower()
        assert db_touched is False, "Database must NOT be accessed when signature check fails"


# Case 4: A valid payment.captured event updates correct order to paid and records payment ID
def test_payment_captured_updates_order_to_paid(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_pay_cap"
    mock_rzp_pay_id = "pay_captured_999"

    payload_bytes = json.dumps(
        make_payment_captured_payload(mock_rzp_order_id, mock_rzp_pay_id, amount=15000)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    marked_paid_args = []
    def mock_mark_paid(conn, rzp_order_id, rzp_pay_id):
        marked_paid_args.append((rzp_order_id, rzp_pay_id))
        return {"id": mock_order_id, "status": "paid"}


    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": mock_order_id, "status": "created", "total_paise": 15000, "items": []},
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


# Amount check: Amount mismatch does NOT mark paid and returns status amount_mismatch
def test_payment_captured_amount_mismatch_leaves_order_created(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_amount_mismatch"

    # Webhook payload claims 10000 paise was paid, but order expects 15000 paise
    payload_bytes = json.dumps(
        make_payment_captured_payload(mock_rzp_order_id, "pay_underpaid", amount=10000)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    order_marked_paid = False
    def unexpected_mark_paid(*args, **kwargs):
        nonlocal order_marked_paid
        order_marked_paid = True

    event_recorded = False
    def mock_record_event(conn, eid, et):
        nonlocal event_recorded
        event_recorded = True
        return True

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", mock_record_event)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": mock_order_id, "status": "created", "total_paise": 15000},
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", unexpected_mark_paid)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_mismatch_1",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "amount_mismatch"
    assert response.json()["expected_paise"] == 15000
    assert response.json()["received_paise"] == 10000
    assert order_marked_paid is False, "Order must NOT be marked as paid when amounts mismatch"
    assert event_recorded is True, "Event must be recorded to prevent duplicate delivery loops"


# Case 5 (Updated): payment.failed must NOT change order status and must NOT restore stock
def test_payment_failed_leaves_order_created_and_stock_untouched(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_pay_fail"
    mock_rzp_pay_id = "pay_failed_456"

    payload_bytes = json.dumps(
        make_payment_failed_payload(mock_rzp_order_id, mock_rzp_pay_id)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    event_recorded = False
    def mock_record_event(conn, eid, et):
        nonlocal event_recorded
        event_recorded = True
        return True

    order_updated = False
    def unexpected_order_update(*args, **kwargs):
        nonlocal order_updated
        order_updated = True

    stock_restored = False
    def unexpected_stock_restore(*args, **kwargs):
        nonlocal stock_restored
        stock_restored = True

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", mock_record_event)
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {
            "id": mock_order_id,
            "status": "created",
            "total_paise": 15000,
            "items": [{"product_id": uuid.uuid4(), "quantity": 2}],
        },
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", unexpected_order_update)

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
    assert response.json()["status"] == "processed"
    assert response.json()["event"] == "payment.failed"
    assert event_recorded is True
    assert order_updated is False, "payment.failed must NOT change order status"
    assert stock_restored is False, "payment.failed must NOT restore stock (customer may retry)"


# Multi-event: payment.captured and order.paid both arrive for same payment
def test_duplicate_paid_events_second_event_is_already_paid(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_double_paid"

    payload_bytes = json.dumps(
        make_order_paid_payload(mock_rzp_order_id, amount=15000)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    marked_paid_called = False
    def unexpected_mark_paid(*args, **kwargs):
        nonlocal marked_paid_called
        marked_paid_called = True

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    # Order is ALREADY in 'paid' status
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": mock_order_id, "status": "paid", "total_paise": 15000},
    )
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", unexpected_mark_paid)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_order_paid_2",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "already_paid"
    assert response.json()["order_id"] == str(mock_order_id)
    assert marked_paid_called is False, "mark_order_paid must not be called if already paid"


# Failed-then-captured retry flow: payment.failed then payment.captured on retry ends in 'paid'
def test_failed_then_captured_leaves_stock_intact_and_order_paid(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_retry_flow"

    current_order_status = "created"

    def mock_get_order(conn, r_id):
        return {"id": mock_order_id, "status": current_order_status, "total_paise": 15000}

    def mock_mark_paid(conn, r_id, p_id):
        nonlocal current_order_status
        current_order_status = "paid"
        return {"id": mock_order_id, "status": "paid"}


    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    monkeypatch.setattr(webhook_service.order_repo, "get_order_by_razorpay_order_id", mock_get_order)
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", mock_mark_paid)

    # 1. First event: payment.failed
    fail_bytes = json.dumps(make_payment_failed_payload(mock_rzp_order_id)).encode("utf-8")
    sig_fail = generate_signature(fail_bytes)
    res1 = client.post(
        "/api/webhooks/razorpay",
        content=fail_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig_fail,
            "X-Razorpay-Event-Id": "evt_fail_1",
        },
    )
    assert res1.status_code == 200
    assert current_order_status == "created", "Order must stay in 'created' after payment.failed"

    # 2. Customer retries payment: payment.captured arrives
    cap_bytes = json.dumps(make_payment_captured_payload(mock_rzp_order_id, amount=15000)).encode("utf-8")
    sig_cap = generate_signature(cap_bytes)
    res2 = client.post(
        "/api/webhooks/razorpay",
        content=cap_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig_cap,
            "X-Razorpay-Event-Id": "evt_cap_2",
        },
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "processed"
    assert current_order_status == "paid", "Order must successfully transition to 'paid'"


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
        make_payment_captured_payload("order_rollback_test", amount=15000)
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
        lambda conn, r_id: {"id": uuid.uuid4(), "status": "created", "total_paise": 15000},
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


# Order in 'failed' status: payment.captured must NOT mark it paid and reply must NOT be 'processed'
def test_payment_captured_for_failed_order_not_processed(client, monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", DUMMY_WEBHOOK_SECRET)

    mock_order_id = uuid.uuid4()
    mock_rzp_order_id = "order_rzp_failed_status"

    payload_bytes = json.dumps(
        make_payment_captured_payload(mock_rzp_order_id, amount=15000)
    ).encode("utf-8")
    sig = generate_signature(payload_bytes)

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(webhook_service, "transaction", fake_tx)
    monkeypatch.setattr(webhook_service.webhook_repo, "record_webhook_event", lambda conn, eid, et: True)
    # Order is in 'failed' status in the database
    monkeypatch.setattr(
        webhook_service.order_repo,
        "get_order_by_razorpay_order_id",
        lambda conn, r_id: {"id": mock_order_id, "status": "failed", "total_paise": 15000},
    )
    # mark_order_paid returns None because status != 'created'
    monkeypatch.setattr(webhook_service.order_repo, "mark_order_paid", lambda conn, r_id, p_id: None)

    response = client.post(
        "/api/webhooks/razorpay",
        content=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": "evt_failed_order_pay",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] != "processed"
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "order_status_failed"

