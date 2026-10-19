import hashlib
import hmac
import json
import logging
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.database import transaction
from app.repositories import order_repo, webhook_repo

logger = logging.getLogger(__name__)


# Verifies the Razorpay webhook signature using HMAC-SHA256 comparison against the exact raw request bytes.
# Protects the system against forged webhook events by requiring the shared webhook secret.
def verify_webhook_signature(
    raw_body: bytes, signature: str | None, secret: str
) -> bool:
    if not signature or not secret:
        return False
    expected_signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)


# Processes an incoming Razorpay webhook event atomically inside a single database transaction.
# Verifies signature, guards against duplicate event IDs, verifies payment amount, and safely handles retries.
def process_razorpay_webhook(
    raw_body: bytes,
    signature: str | None,
    event_id: str | None,
) -> dict:
    # Step 1: Verify HMAC-SHA256 signature using the raw HTTP request body
    if not verify_webhook_signature(raw_body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook signature",
        )

    # Step 2: Validate payload structure
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must be a JSON object",
        )

    # Step 3: Validate event ID (check header first, then fallback to payload event_id)
    resolved_event_id = event_id or payload.get("event_id") or payload.get("id")
    if not resolved_event_id or not str(resolved_event_id).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing event ID header or payload field",
        )
    resolved_event_id = str(resolved_event_id).strip()

    event_type = payload.get("event")
    if not event_type or not isinstance(event_type, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid event field in payload",
        )

    # Step 4: Atomic processing inside a single database transaction
    with transaction() as conn:
        # Deduplication check: record event ID; if already present, ignore duplicate delivery
        is_new_event = webhook_repo.record_webhook_event(
            conn, resolved_event_id, event_type
        )
        if not is_new_event:
            return {
                "status": "ignored",
                "reason": "duplicate_event",
                "event_id": resolved_event_id,
            }

        # Extract payment entity details from payload
        payload_data = payload.get("payload", {})
        if not isinstance(payload_data, dict):
            payload_data = {}

        payment_entity = payload_data.get("payment", {}).get("entity", {})
        razorpay_payment_id = payment_entity.get("id")
        razorpay_order_id = payment_entity.get("order_id")
        paid_amount = payment_entity.get("amount")

        if not razorpay_order_id:
            order_entity = payload_data.get("order", {}).get("entity", {})
            razorpay_order_id = order_entity.get("id")
            if paid_amount is None:
                paid_amount = order_entity.get("amount")

        if not razorpay_order_id:
            return {
                "status": "ignored",
                "reason": "missing_razorpay_order_id",
                "event_id": resolved_event_id,
            }

        # Handle payment capture / order paid events
        if event_type in ("payment.captured", "order.paid"):
            order = order_repo.get_order_by_razorpay_order_id(conn, razorpay_order_id)
            if not order:
                return {
                    "status": "ignored",
                    "reason": "order_not_found",
                    "razorpay_order_id": razorpay_order_id,
                }

            if order["status"] == "paid":
                return {
                    "status": "already_paid",
                    "order_id": str(order["id"]),
                    "razorpay_order_id": razorpay_order_id,
                }

            # Verify amount matches expected total_paise
            if paid_amount is None or paid_amount != order["total_paise"]:
                logger.warning(
                    "Webhook amount mismatch for order %s (razorpay_order_id=%s): expected %s paise, received %s paise",
                    order["id"],
                    razorpay_order_id,
                    order["total_paise"],
                    paid_amount,
                )
                return {
                    "status": "amount_mismatch",
                    "order_id": str(order["id"]),
                    "expected_paise": order["total_paise"],
                    "received_paise": paid_amount,
                }

            updated_order = order_repo.mark_order_paid(conn, razorpay_order_id, razorpay_payment_id)
            if not updated_order:
                return {
                    "status": "ignored",
                    "reason": f"order_status_{order['status']}",
                    "order_id": str(order["id"]),
                    "razorpay_order_id": razorpay_order_id,
                }

            return {
                "status": "processed",
                "event": event_type,
                "order_id": str(order["id"]),
                "razorpay_order_id": razorpay_order_id,
            }


        # Handle payment failed events
        elif event_type == "payment.failed":
            order = order_repo.get_order_by_razorpay_order_id(conn, razorpay_order_id)
            if not order:
                return {
                    "status": "ignored",
                    "reason": "order_not_found",
                    "razorpay_order_id": razorpay_order_id,
                }

            # Razorpay allows customer payment retries on the same order.
            # Do NOT fail order and do NOT restore stock to prevent inventory overselling.
            return {
                "status": "processed",
                "event": event_type,
                "order_id": str(order["id"]),
                "razorpay_order_id": razorpay_order_id,
            }

        # Safe fallback for other webhook events
        return {
            "status": "ignored",
            "reason": "unhandled_event",
            "event": event_type,
        }

