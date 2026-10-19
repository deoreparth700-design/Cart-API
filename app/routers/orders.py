from fastapi import APIRouter, Depends

from app.core.security import get_current_user

router = APIRouter(tags=["orders"])


# Returns the authenticated user's ID extracted from the verified JWT token.
# Serves as a test endpoint to ensure JWT verification against Project 2 auth-service works before building order endpoints.
@router.get("/whoami")
def whoami(user_id=Depends(get_current_user)):
    return {"user_id": user_id}
