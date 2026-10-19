from fastapi import APIRouter, Header, Request, status

from app.services import webhook_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Handles incoming HTTP POST webhooks from Razorpay.
# Reads the raw request body and signature headers, delegates atomic processing to the service, and returns 200 on success.
@router.post("/razorpay", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    x_razorpay_event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
):
    raw_body = await request.body()
    return webhook_service.process_razorpay_webhook(
        raw_body=raw_body,
        signature=x_razorpay_signature,
        event_id=x_razorpay_event_id,
    )
