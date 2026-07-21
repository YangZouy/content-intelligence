"""
Base Publisher Protocol - Defines the interface for all platform publishers.

Every publisher (GitHub Pages, WeChat, etc.) must implement this Protocol.
The PublishNode uses this interface to interact with publishers uniformly,
enabling independent parallel publishing with separate retry logic.

Protocol Methods:
    - platform: Property returning platform identifier string
    - validate: Pre-publish content validation
    - publish: Execute the actual publish operation
    - retry: Retry a failed publish with exponential backoff
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

from src.state import BrandConfig, PublishResultItem


@runtime_checkable
class Publisher(Protocol):
    """Protocol defining the required interface for content publishers.

    All concrete publisher implementations must conform to this protocol.
    The protocol enables:
    - Polymorphic handling in PublishNode
    - Independent retry logic per publisher
    - Platform-agnostic orchestration
    """

    @property
    def platform(self) -> str:
        """Return the platform identifier string.

        Used for logging, result tracking, and user display.
        Must be one of: 'blog', 'wechat', or other registered platforms.
        """
        ...

    def validate(
        self,
        content: str,
        brand: BrandConfig,
    ) -> tuple[bool, List[str]]:
        """Validate content before publishing.

        Checks that content meets the platform's requirements:
        - Minimum length
        - Required fields present
        - Format compliance

        Args:
            content: Full document content to validate.
            brand: Brand configuration with constraints.

        Returns:
            Tuple of (is_valid, list_of_error_messages).
        """
        ...

    def publish(
        self,
        content: str,
        brand: BrandConfig,
    ) -> PublishResultItem:
        """Execute the publish operation for this platform.

        This is the core method that performs the actual publishing work.
        Each implementation handles its own API/protocol details internally.

        Args:
            content: Platform-formatted document to publish.
            brand: Brand configuration.

        Returns:
            PublishResultItem with success status, URL (if available),
            and attempt count.
        """
        ...

    def retry(
        self,
        prev_result: PublishResultItem,
        max_attempts: int = 3,
    ) -> PublishResultItem:
        """Retry a failed publish operation with exponential backoff.

        Uses the retry_with_backoff decorator logic internally.
        Implements independent retry — failures on one platform don't affect others.

        Args:
            prev_result: The previous failed PublishResultItem.
            max_attempts: Maximum total attempts including the first.

        Returns:
            New PublishResultItem after retries (may still be failed).
        """
        ...
