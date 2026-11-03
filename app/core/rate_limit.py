from collections import defaultdict
import time
from fastapi import HTTPException, Request, status

DEMO_TOKEN_RATE_LIMIT = 5
DEMO_TOKEN_WINDOW_SECONDS = 60
RATE_LIMIT_ERROR_DETAIL = "Too many demo token requests. Please try again later."

_ip_request_timestamps: dict[str, list[float]] = defaultdict(list)


def check_demo_token_rate_limit(request: Request, current_time: float | None = None) -> None:
    """
    In-memory rate limiter: max 5 requests per 60 seconds per client IP.
    Uses request.client.host as the identifier.
    Raises HTTP 429 when the rate limit is exceeded.
    Prunes stale IP entries on each call so memory cannot grow without bound.
    """
    client_ip = request.client.host if request.client and request.client.host else "127.0.0.1"
    now = current_time if current_time is not None else time.time()
    cutoff = now - DEMO_TOKEN_WINDOW_SECONDS

    # Prune stale entries across all IPs so memory cannot grow without bound
    expired_ips = [
        ip for ip, ts_list in _ip_request_timestamps.items()
        if not ts_list or ts_list[-1] <= cutoff
    ]
    for ip in expired_ips:
        del _ip_request_timestamps[ip]

    timestamps = [ts for ts in _ip_request_timestamps.get(client_ip, []) if ts > cutoff]
    _ip_request_timestamps[client_ip] = timestamps

    if len(timestamps) >= DEMO_TOKEN_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMIT_ERROR_DETAIL,
        )

    _ip_request_timestamps[client_ip].append(now)


def reset_rate_limits() -> None:
    """Clears in-memory rate limit records for test isolation."""
    _ip_request_timestamps.clear()
