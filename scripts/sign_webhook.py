#!/usr/bin/env python3
# Helper script to send signed Razorpay webhook payloads to the Cart API endpoint.
# Reads raw bytes from a JSON payload file, computes an HMAC-SHA256 signature using the secret from .env,
# and sends the request using httpx. Supports tamper mode to simulate signature mismatches.

import argparse
import hashlib
import hmac
import os
import sys
import uuid
from pathlib import Path
import httpx

# Resolve project root so configuration can be imported from app.core.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings


# Loads the Razorpay webhook secret from application settings or directly from the .env file.
# Ensures the secret is read securely without ever logging or printing it.
def load_webhook_secret() -> str:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if secret:
        return secret
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("RAZORPAY_WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip().strip('"\'')
    return ""


# Generates an HMAC-SHA256 hex digest signature from raw payload bytes.
# Uses constant-time generation for cryptographic integrity.
def compute_signature(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()


# Sends raw bytes to the webhook endpoint with appropriate signature and event ID headers.
# Prints HTTP status code and response body while strictly redacting all secrets.
def send_webhook(
    file_path: str,
    event_id: str | None = None,
    url: str = "http://127.0.0.1:8000/api/webhooks/razorpay",
    tamper: bool = False,
) -> None:
    secret = load_webhook_secret()
    if not secret:
        print("Error: RAZORPAY_WEBHOOK_SECRET not found in environment or .env file.", file=sys.stderr)
        sys.exit(1)

    path = Path(file_path)
    if not path.is_file():
        print(f"Error: Payload file '{file_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Read exactly as raw bytes
    raw_bytes = path.read_bytes()

    # Sign the exact raw bytes
    signature = compute_signature(raw_bytes, secret)

    # Assign event ID if not provided
    resolved_event_id = event_id or f"evt_cli_{uuid.uuid4().hex[:12]}"

    # If tampering is enabled, alter the body bytes AFTER signing
    body_to_send = raw_bytes + b" " if tamper else raw_bytes

    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": resolved_event_id,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                url,
                content=body_to_send,
                headers=headers,
            )
            print(f"Status Code: {response.status_code}")
            print(f"Response Body: {response.text}")
    except Exception as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        sys.exit(1)


# Main CLI entry point parsing command-line flags and executing webhook delivery.
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sign and send raw JSON webhook payloads to Cart API"
    )
    parser.add_argument("file", help="Path to the JSON payload file")
    parser.add_argument("--event-id", help="Razorpay event ID header (default: auto-generated)")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/api/webhooks/razorpay",
        help="Target webhook URL (default: http://127.0.0.1:8000/api/webhooks/razorpay)",
    )
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="Modify the body after signing to trigger 401 signature verification failure",
    )

    args = parser.parse_args()
    send_webhook(
        file_path=args.file,
        event_id=args.event_id,
        url=args.url,
        tamper=args.tamper,
    )


if __name__ == "__main__":
    main()
