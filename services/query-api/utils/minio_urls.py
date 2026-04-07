"""MinIO signed URL generation.

Per security-design.md:
- User-facing downloads use signed GET URLs only
- Default expiry: 1 hour
- Signed URLs are issued by the API layer after authorization checks
- Browsers MUST NEVER receive raw MinIO credentials
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

logger = logging.getLogger(__name__)


def create_minio_client(
    endpoint: str,
    access_key: str,
    secret_key: str,
    secure: bool = False,
) -> Any:
    """Create a MinIO client instance."""
    try:
        from minio import Minio  # noqa: PLC0415
    except ImportError:
        logger.warning("minio package not installed — signed URLs disabled")
        return None

    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def generate_signed_url(
    client: Any,
    uri: str,
    expiry_s: int = 3600,
) -> str | None:
    """Generate a signed GET URL for an s3:// URI.

    Args:
        client: MinIO client instance.
        uri: Object URI in ``s3://bucket/path`` format.
        expiry_s: URL expiry in seconds (default 1 hour).

    Returns:
        Signed URL string, or None if generation fails.
    """
    if client is None or not uri:
        return None

    # Parse s3://bucket/path
    if uri.startswith("s3://"):
        uri_path = uri[5:]
    else:
        uri_path = uri

    parts = uri_path.split("/", 1)
    if len(parts) != 2:
        logger.warning("Invalid URI for signing: %s", uri)
        return None

    bucket, object_name = parts

    try:
        url = client.presigned_get_object(
            bucket,
            object_name,
            expires=timedelta(seconds=expiry_s),
        )
        return url
    except Exception:
        logger.warning("Failed to generate signed URL for %s", uri, exc_info=True)
        return None
