from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt

from app.core.config import settings

# HTTPBearer scheme extracts the Bearer token from the Authorization header.
# Setting auto_error=False allows us to provide custom, consistent 401 error responses for missing credentials.
bearer_scheme = HTTPBearer(auto_error=False)


# Verifies the JWT Bearer token and extracts the authenticated user ID.
# Protects private endpoints by rejecting requests with missing, expired, or tampered tokens with HTTP 401.
def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> int:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Project 2 auth-service stores the user id under the 'userId' claim.
    user_id = payload.get("userId") or payload.get("sub") or payload.get("user_id")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload is missing user ID",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return int(user_id)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID format in token",
            headers={"WWW-Authenticate": "Bearer"},
        )
