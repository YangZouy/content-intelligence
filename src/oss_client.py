"""
Aliyun OSS Client - Image upload and URL generation.

Provides a unified interface for uploading images (local files or URLs)
to Aliyun OSS, with pypinyin-based English path generation.

Usage:
    from src.oss_client import get_oss_client
    client = get_oss_client()
    url = client.upload_local_file("/path/to/image.png", "images/ArticleName/image.png")
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional, Protocol, Tuple
from urllib.parse import urlparse

import httpx
import oss2
from PIL import Image
from pypinyin import Style, pinyin

from src.config_loader import get_config
from src.errors import OSSError, retry_with_backoff


class OSSClient(Protocol):
    """Protocol defining the required interface for any OSS client implementation."""

    def upload_local_file(
        self,
        local_path: str,
        object_key: Optional[str] = None,
    ) -> str:
        """Upload a local file to OSS and return the public URL.

        Args:
            local_path: Absolute path to the local file.
            object_key: Target OSS object key. If None, auto-generated.

        Returns:
            Public URL of the uploaded object.
        """
        ...

    def upload_from_url(
        self,
        image_url: str,
        object_key: Optional[str] = None,
    ) -> str:
        """Download an image from URL and upload it to OSS.

        Args:
            image_url: Public URL of the source image.
            object_key: Target OSS object key. If None, auto-generated.

        Returns:
            Public URL of the uploaded object in OSS.
        """
        ...

    def generate_object_key(
        self,
        title_abbr: str,
        filename: str,
    ) -> str:
        """Generate an OSS object key with English path from Chinese title.

        Uses pypinyin to convert Chinese characters to pinyin abbreviations
        for the directory name, ensuring all paths are ASCII-safe.

        Args:
            title_abbr: Short title or identifier (may contain Chinese).
            filename: Original filename with extension.

        Returns:
            OSS object key like 'images/ShenDuXueXiRu/image001.png'.
        """
        ...

    def is_oss_url(self, url: str) -> bool:
        """Check if a URL is already an OSS URL for this bucket."""
        ...


class AliyunOSSClient:
    """Concrete implementation of OSSClient using Aliyun OSS SDK (oss2).

    Handles:
    - Local file uploads
    - Remote URL downloads + re-upload
    - Automatic English path generation via pypinyin
    - File type validation
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        bucket_name: Optional[str] = None,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
    ):
        """Initialize the OSS client with credentials.

        Args:
            endpoint: OSS endpoint URL. Defaults to config value.
            bucket_name: Bucket name. Defaults to config value.
            access_key_id: Access key ID. Defaults to config value.
            access_key_secret: Access key secret. Defaults to config value.
        """
        config = get_config()
        self._endpoint = endpoint or config.get("oss", "endpoint", "")
        self._bucket_name = bucket_name or config.get("oss", "bucket_name", "")
        self._access_key_id = (
            access_key_id or config.get("oss", "access_key_id", "")
        )
        self._access_key_secret = (
            access_key_secret or config.get("oss", "access_key_secret", "")
        )
        self._allowed_extensions = set(
            config.oss.get("allowed_extensions", [".jpg", ".jpeg", ".png"])
        )
        self._image_path_prefix = config.oss.get(
            "image_path_prefix", "images"
        )

        # Initialize auth and bucket (lazy - only when first used)
        self._auth: Optional[oss2.Auth] = None
        self._bucket: Optional[oss2.Bucket] = None

    def _ensure_bucket(self) -> oss2.Bucket:
        """Lazy-initialize the oss2.Bucket instance.

        Returns:
            Initialized oss2.Bucket object.

        Raises:
            OSSError: If credentials are not configured.
        """
        if self._bucket is not None:
            return self._bucket

        if not all([self._access_key_id, self._access_key_secret, self._endpoint, self._bucket_name]):
            raise OSSError(
                "OSS credentials not fully configured. "
                "Please check .env file or config.yaml."
            )

        self._auth = oss2.Auth(self._access_key_id, self._access_key_secret)
        self._bucket = oss2.Bucket(
            self._auth, self._endpoint, self._bucket_name
        )
        return self._bucket

    def _get_public_url(self, object_key: str) -> str:
        """Construct the public HTTPS URL for an uploaded object.

        Args:
            object_key: The OSS object key.

        Returns:
            Full public URL like 'https://bucket.oss-cn-hangzhou.aliyuncs.com/images/...'.
        """
        # Parse endpoint to extract region for URL construction
        if self._endpoint.startswith("https://"):
            base_url = f"https://{self._bucket_name}.{self._endpoint[len('https://'):]}"
        elif self._endpoint.startswith("http://"):
            base_url = f"http://{self._bucket_name}.{self._endpoint[len('http://'):]}"
        else:
            base_url = f"https://{self._bucket_name}.{self._endpoint}"

        return f"{base_url}/{object_key}"

    def _validate_image_file(self, file_path: str) -> bool:
        """Validate that a file has an allowed image extension.

        Args:
            file_path: Path to the file to validate.

        Returns:
            True if extension is allowed, False otherwise.
        """
        ext = Path(file_path).suffix.lower()
        return ext in self._allowed_extensions

    @retry_with_backoff(max_attempts=3, base_delay=1.0)
    def upload_local_file(
        self,
        local_path: str,
        object_key: Optional[str] = None,
    ) -> str:
        """Upload a local file to OSS.

        Validates file extension, reads content, uploads to the configured
        bucket, and returns the public URL.

        Args:
            local_path: Absolute or relative path to the local image file.
            object_key: Target OSS key. Auto-generated if None.

        Returns:
            Public URL of the uploaded image.

        Raises:
            OSSError: If file doesn't exist, invalid format, or upload fails.
        """
        path = Path(local_path)
        if not path.exists():
            raise OSSError(f"Local file not found: {local_path}")

        if not self._validate_image_file(local_path):
            raise OSSError(
                f"Unsupported file type: {path.suffix}. "
                f"Allowed: {sorted(self._allowed_extensions)}"
            )

        if object_key is None:
            # Generate object key from filename only (directory part handled by caller)
            object_key = f"{self._image_path_prefix}/{path.name}"

        try:
            bucket = self._ensure_bucket()
            with open(local_path, "rb") as f:
                result = bucket.put_object(object_key, f)

            if result.status != 200:
                raise OSSError(
                    f"OSS upload failed with status {result.status}: {result.read()}"
                )

            return self._get_public_url(object_key)
        except oss2.exceptions.OssError as e:
            raise OSSError(f"OSS error during upload: {e}") from e

    @retry_with_backoff(max_attempts=3, base_delay=1.0)
    def upload_from_url(
        self,
        image_url: str,
        object_key: Optional[str] = None,
    ) -> str:
        """Download an image from a remote URL and re-upload to OSS.

        Downloads the image data, optionally validates it as a real image,
        then uploads to OSS.

        Args:
            image_url: Source image URL (HTTP/HTTPS).
            object_key: Target OSS key. Auto-generated if None.

        Returns:
            Public URL of the uploaded image in OSS.

        Raises:
            OSSError: If download or upload fails.
        """
        try:
            # Download with timeout
            response = httpx.get(image_url, timeout=30, follow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            image_data = response.content

            # Validate it's actually an image by trying to open with Pillow
            try:
                img = Image.open(BytesIO(image_data))
                img.verify()  # Verify it's a valid image
            except Exception:
                raise OSSError(
                    f"URL does not point to a valid image: {image_url}"
                )

            # Determine extension from content-type or URL
            ext = self._guess_extension(content_type, image_url)
            filename = f"{self._hash_url(image_url)}{ext}"

            if object_key is None:
                object_key = f"{self._image_path_prefix}/{filename}"

            # Upload the downloaded bytes
            bucket = self._ensure_bucket()
            result = bucket.put_object(object_key, image_data)

            if result.status != 200:
                raise OSSError(
                    f"OSS upload failed with status {result.status}"
                )

            return self._get_public_url(object_key)

        except httpx.HTTPError as e:
            raise OSSError(f"Failed to download image from {image_url}: {e}") from e

    def generate_object_key(
        self,
        title_abbr: str,
        filename: str,
    ) -> str:
        """Generate an OSS object key with pinyin-based English directory.

        Converts Chinese title characters to their pinyin initials
        to create an ASCII-safe directory name.

        Examples:
            '深度学习入门' -> 'images/ShenDuXueXiRuM/photo.png'
            'AI实战' -> 'images/AiShiZhan/chart.jpg'

        Args:
            title_abbr: Title text (may contain Chinese characters).
            filename: Original filename with extension.

        Returns:
            Full object key string.
        """
        # Generate pinyin abbreviation from title
        py_list = pinyin(title_abbr, style=Style.FIRST_LETTER)
        dir_name = "".join([item[0].upper() for item in py_list if item])

        # Clean up: keep only alphanumeric
        import re
        dir_name = re.sub(r"[^A-Za-z0-9]", "", dir_name)
        if not dir_name:
            dir_name = "Untitled"

        # Sanitize filename
        safe_filename = Path(filename).name
        safe_filename = re.sub(r"[^A-Za-z0-9._-]", "", safe_filename)
        if not safe_filename:
            safe_filename = "image.png"

        return f"{self._image_path_prefix}/{dir_name}/{safe_filename}"

    def is_oss_url(self, url: str) -> bool:
        """Check whether a URL points to this OSS bucket.

        Args:
            url: URL string to check.

        Returns:
            True if the URL belongs to this OSS bucket.
        """
        if not url:
            return False
        parsed = urlparse(url)
        return (
            self._bucket_name in (parsed.hostname or "")
            or self._bucket_name in url
        )

    @staticmethod
    def _hash_url(url: str) -> str:
        """Generate a short hash from a URL for use as a filename prefix."""
        return hashlib.md5(url.encode()).hexdigest()[:12]

    @staticmethod
    def _guess_extension(content_type: str, url: str) -> str:
        """Guess file extension from content-type header or URL path."""
        # Try content-type first
        ct_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
        }
        if content_type:
            for ct, ext in ct_map.items():
                if ct in content_type:
                    return ext

        # Fallback to URL extension
        parsed = urlparse(url)
        ext = Path(parsed.path).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
            return ext

        return ".png"  # Default fallback


# Module-level singleton cache
_oss_client_instance: Optional[AliyunOSSClient] = None


def get_oss_client(force_new: bool = False) -> AliyunOSSClient:
    """Get the singleton OSS client instance.

    Args:
        force_new: If True, create a new instance (useful after config changes).

    Returns:
        Shared AliyunOSSClient instance.
    """
    global _oss_client_instance
    if _oss_client_instance is None or force_new:
        _oss_client_instance = AliyunOSSClient()
    return _oss_client_instance
