import jwt
from fastapi import APIRouter

from app.core.config import settings

# Demo-only router: issues a JWT for a hardcoded userId so the storefront
# can call protected endpoints without a real registration / login flow.
# include_in_schema=False keeps this out of the public Swagger docs.
router = APIRouter(tags=["demo"])


@router.get("/demo-token", include_in_schema=False)
def get_demo_token():
    """
    Returns a short-lived demo JWT for userId=1.
    Used exclusively by the storefront demo page — not for production use.
    """
    token = jwt.encode(
        {"userId": 1},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    return {"token": token}
