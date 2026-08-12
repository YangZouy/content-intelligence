"""
Error Hierarchy and Retry Decorator for Content Intelligence Dispatcher.

Defines a structured exception hierarchy and a configurable retry decorator
with exponential backoff, used throughout the pipeline.

Hierarchy:
    CIDError (base)
    ├── IngestError        - Content import failures
    ├── FormatError        - Formatting rule engine failures
    ├── LLMError           - LLM API call failures
    ├── OSSError           - OSS upload/download failures
    ├── PublishError       - General publishing failures
    │   └── GitHubPublishError  - GitHub-specific publish failures
    └── WeChatError        - WeChat/wenyan-mcp communication failures
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Optional, Type, TypeVar


class CIDError(Exception):
    """Base exception for all Content Intelligence Dispatcher errors.

    All custom exceptions in this system inherit from this class,
    allowing callers to catch any dispatcher error with a single except clause.
    """

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        """Initialize with an optional details dict for structured error info.

        Args:
            message: Human-readable error description.
            details: Optional dictionary with additional context.
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | details={self.details}"
        return self.message


class IngestError(CIDError):
    """Raised when content ingestion fails (file not found, unsupported format, etc.)."""
    pass


class FormatError(CIDError):
    """Raised when the format optimization rule engine encounters an error."""
    pass


class LLMError(CIDError):
    """Raised when LLM API calls fail (network, auth, rate limit, etc.)."""
    pass


class OSSError(CIDError):
    """Raised when OSS operations fail (upload, download, auth)."""
    pass


class PublishError(CIDError):
    """Raised when general publishing operations fail."""

    def __init__(
        self,
        message: str,
        platform: str = "",
        attempt: int = 0,
        details: Optional[dict] = None,
    ) -> None:
        super().__init__(message, details)
        self.platform = platform
        self.attempt = attempt


class GitHubPublishError(PublishError):
    """Raised specifically by the GitHub Pages publisher."""
    pass


class WeChatError(CIDError):
    """Raised when WeChat publishing via wenyan-mcp fails."""
    pass


# --- Retry Decorator ---

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    retryable_errors: Optional[tuple[Type[Exception], ...]] = None,
) -> Callable[[F], F]:
    """Decorator that retries a function on failure with exponential backoff.

    Retries the wrapped function up to `max_attempts` times. Each retry waits
    `base_delay * (exponential_base ** attempt)` seconds, capped at `max_delay`.

    Only exceptions in `retryable_errors` are retried; others are re-raised immediately.
    If `retryable_errors` is None, all CIDError subclasses are retried.

    Usage:
        @retry_with_backoff(max_attempts=3)
        def upload_file(path): ...

    Args:
        max_attempts: Maximum number of attempts (including the first).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Maximum cap on delay between retries.
        exponential_base: Multiplier for each subsequent delay.
        retryable_errors: Tuple of exception types to catch and retry.
            Defaults to (CIDError,).

    Returns:
        Decorated function with retry logic.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Check if this exception type should be retried
                    target_errors = (
                        retryable_errors or (CIDError, OSError, ConnectionError)
                    )
                    if not isinstance(e, target_errors):
                        raise

                    # Don't sleep after last attempt
                    if attempt < max_attempts:
                        delay = min(
                            base_delay * (exponential_base ** (attempt - 1)),
                            max_delay,
                        )
                        from src.observability import get_trace_logger
                        get_trace_logger().record_retry(
                            operation=func.__qualname__,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            error=e,
                            delay_seconds=delay,
                        )
                        time.sleep(delay)

            # Exhausted all attempts - re-raise the last exception
            raise last_exception  # type: ignore

        return wrapper  # type: ignore

    return decorator
