from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, status
import jwt

from app.core.config import settings

# Fixed identifier for the demo user.
# Using 999999 ensures demo activity is completely separated from real registered users.
DEMO_USER_ID = 999999

# Demo tokens automatically expire after 30 minutes.
DEMO_TOKEN_EXPIRY_MINUTES = 30

router = APIRouter(tags=["demo"])


# Issues a short-lived demo JWT when DEMO_MODE is enabled.
# Returns 404 Not Found if DEMO_MODE is false to prevent unauthorized access in production.
# The endpoint does not accept any query or body arguments, so user ID and expiry cannot be tampered with.
@router.get("/demo-token", include_in_schema=False)
def get_demo_token():
    """
    Returns a short-lived demo JWT for testing the storefront.
    Disabled in production unless DEMO_MODE is explicitly set to true.
    """
    if not settings.DEMO_MODE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo mode is disabled",
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=DEMO_TOKEN_EXPIRY_MINUTES)

    token = jwt.encode(
        {
            "userId": DEMO_USER_ID,
            "iat": now,
            "exp": expires_at,
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    return {"token": token}
