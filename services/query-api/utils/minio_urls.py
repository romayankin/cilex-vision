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
    region: str | None = None,
) -> Any:
    """Create a MinIO client instance.

    Pass ``region`` to skip the region-lookup round trip on first use —
    required when the client is only reachable for signing (not for network
    calls), e.g., the request-hostname client used to rewrite presigned URLs.
    """
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
        region=region,
    )


def generate_signed_url(
    client: Any,
    uri: str,
    expiry_s: int = 3600,
    request: Any = None,
) -> str | None:
    """Generate a signed GET URL for an s3:// URI.

    Args:
        client: MinIO client instance (uses the Docker-internal endpoint).
        uri: Object URI in ``s3://bucket/path`` format.
        expiry_s: URL expiry in seconds (default 1 hour).
        request: Optional FastAPI Request; if provided, a short-lived MinIO
            client is constructed using the hostname the browser used to
            reach the API (on port 9000) so the presigned URL is reachable
            from hosts outside the compose network. Re-signing with the
            external endpoint is required because SigV4 signs the host
            header — a naive string replace breaks the signature.

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

    signing_client = client
    if request is not None:
        try:
            settings = request.app.state.settings.minio
            host_header = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
            external_host = host_header.split(":")[0]
            external_client = create_minio_client(
                endpoint=f"{external_host}:9000",
                access_key=settings.access_key,
                secret_key=settings.secret_key,
                secure=settings.secure,
                region="us-east-1",
            )
            if external_client is not None:
                signing_client = external_client
        except Exception:
            logger.warning("Failed to build external signing client; using internal", exc_info=True)

    try:
        return signing_client.presigned_get_object(
            bucket,
            object_name,
            expires=timedelta(seconds=expiry_s),
        )
    except Exception:
        logger.warning("Failed to generate signed URL for %s", uri, exc_info=True)
        return None
