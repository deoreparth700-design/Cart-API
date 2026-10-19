import razorpay

from app.core.config import settings


# Creates an order on Razorpay using the official SDK client.
# Configures the order in INR currency with the total amount in paise and assigns our internal order ID as the receipt reference.
def create_razorpay_order(total_paise: int, receipt: str) -> str:
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )
    order_data = {
        "amount": total_paise,
        "currency": "INR",
        "receipt": str(receipt),
    }
    razorpay_order = client.order.create(data=order_data)
    return razorpay_order["id"]
