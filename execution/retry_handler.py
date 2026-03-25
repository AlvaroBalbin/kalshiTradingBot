"""Retry logic with exponential backoff for API calls."""

import asyncio
from functools import wraps
from typing import Callable, Any

import httpx
import structlog

log = structlog.get_logger()

# Retryable HTTP status codes
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds


class RetryableError(Exception):
    pass


class FatalError(Exception):
    pass


def classify_error(error: Exception) -> str:
    """Classify an error as retryable or fatal."""
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in RETRYABLE_STATUSES:
            return "retryable"
        if error.response.status_code in {401, 403}:
            return "fatal_auth"
        if error.response.status_code == 400:
            return "fatal_bad_request"
        return "fatal"

    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
        return "retryable"

    return "fatal"


def with_retry(max_retries: int = MAX_RETRIES, base_delay: float = BASE_DELAY):
    """Decorator for retrying async functions with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_error = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    classification = classify_error(e)

                    if classification.startswith("fatal"):
                        log.error("fatal_error",
                                  func=func.__name__,
                                  error=str(e),
                                  classification=classification)
                        raise

                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        log.warning("retrying",
                                    func=func.__name__,
                                    attempt=attempt + 1,
                                    delay=delay,
                                    error=str(e))
                        await asyncio.sleep(delay)

            raise last_error

        return wrapper
    return decorator
