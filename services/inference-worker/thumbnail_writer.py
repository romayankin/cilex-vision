"""Per-track thumbnail writer for the inference worker.

Crops each detection from the full frame, JPEG-encodes it, and uploads to
MinIO under a deterministic path. Enforces a per-track budget so busy
tracks don't blow up storage.

Coordinate space: RawDetection uses normalized [0,1] pixel coordinates.
Thumbnails are resized to at most ``max_width`` preserving aspect ratio.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from typing import Any

import numpy as np

from config import ThumbnailConfig
from detector_client import RawDetection

logger = logging.getLogger(__name__)


class ThumbnailWriter:
    """Uploads per-track crops to MinIO with a simple per-track budget."""

    def __init__(
        self,
        cfg: ThumbnailConfig,
        minio_client: Any,
    ) -> None:
        self._cfg = cfg
        self._minio = minio_client
        self._count_by_track: dict[str, int] = {}
        self._bucket_ready = False

    async def ensure_bucket(self) -> None:
        if self._minio is None or self._bucket_ready:
            return
        try:
            exists = await asyncio.to_thread(
                self._minio.bucket_exists, self._cfg.bucket
            )
            if not exists:
                await asyncio.to_thread(self._minio.make_bucket, self._cfg.bucket)
            self._bucket_ready = True
        except Exception:
            logger.warning(
                "Thumbnail bucket setup failed for %s", self._cfg.bucket, exc_info=True
            )

    def clear_track(self, track_id: str) -> None:
        self._count_by_track.pop(track_id, None)

    async def maybe_write(
        self,
        *,
        frame: np.ndarray,
        detection: RawDetection,
        track_id: str,
        camera_id: str,
    ) -> str | None:
        """Upload one thumbnail if the track is under budget.

        Returns the ``s3://bucket/key`` URI on success, or None.
        """
        if not self._cfg.enabled or self._minio is None:
            return None
        if detection.confidence < self._cfg.min_confidence:
            return None
        if self._count_by_track.get(track_id, 0) >= self._cfg.max_per_track:
            return None

        crop = self._crop(frame, detection)
        if crop is None:
            return None

        jpeg = self._encode(crop)
        if jpeg is None:
            return None

        key = f"{camera_id}/{track_id}/{uuid.uuid4().hex}.jpg"
        try:
            await asyncio.to_thread(
                self._minio.put_object,
                self._cfg.bucket,
                key,
                io.BytesIO(jpeg),
                length=len(jpeg),
                content_type="image/jpeg",
            )
        except Exception:
            logger.debug(
                "Thumbnail upload failed for track %s", track_id, exc_info=True
            )
            return None

        self._count_by_track[track_id] = self._count_by_track.get(track_id, 0) + 1
        return f"s3://{self._cfg.bucket}/{key}"

    def _crop(self, frame: np.ndarray, det: RawDetection) -> np.ndarray | None:
        """Crop a detection bbox from the frame with padding + fixed aspect ratio.

        Algorithm:
        1. Compute bbox pixel coords and center
        2. Pad each dimension by ``crop_padding`` fraction
        3. Enforce minimum crop size (``min_crop_ratio`` of frame)
        4. Force ``target_aspect_ratio`` by widening or heightening
        5. Shift instead of clip when bumping into a frame edge — preserves
           the crop size when the subject is near the border
        """
        h, w = frame.shape[:2]
        x0 = int(det.x_min * w)
        y0 = int(det.y_min * h)
        x1 = int(det.x_max * w)
        y1 = int(det.y_max * h)
        if x1 <= x0 or y1 <= y0:
            return None

        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        bw = x1 - x0
        bh = y1 - y0

        padded_w = bw * (1 + 2 * self._cfg.crop_padding)
        padded_h = bh * (1 + 2 * self._cfg.crop_padding)

        min_w = w * self._cfg.min_crop_ratio
        min_h = h * self._cfg.min_crop_ratio
        crop_w = max(padded_w, min_w)
        crop_h = max(padded_h, min_h)

        target = self._cfg.target_aspect_ratio
        current_aspect = crop_w / crop_h
        if current_aspect < target:
            crop_w = crop_h * target
        elif current_aspect > target:
            crop_h = crop_w / target

        half_w = crop_w / 2
        half_h = crop_h / 2
        nx0 = int(cx - half_w)
        ny0 = int(cy - half_h)
        nx1 = int(cx + half_w)
        ny1 = int(cy + half_h)

        if nx0 < 0:
            nx1 = min(w, nx1 - nx0)
            nx0 = 0
        if ny0 < 0:
            ny1 = min(h, ny1 - ny0)
            ny0 = 0
        if nx1 > w:
            nx0 = max(0, nx0 - (nx1 - w))
            nx1 = w
        if ny1 > h:
            ny0 = max(0, ny0 - (ny1 - h))
            ny1 = h

        if nx1 <= nx0 or ny1 <= ny0:
            return None

        return frame[ny0:ny1, nx0:nx1]

    def _encode(self, crop: np.ndarray) -> bytes | None:
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError:
            return None
        try:
            img = Image.fromarray(crop)
            if img.width > self._cfg.max_width:
                ratio = self._cfg.max_width / float(img.width)
                new_h = max(1, int(img.height * ratio))
                img = img.resize((self._cfg.max_width, new_h), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self._cfg.quality)
            return buf.getvalue()
        except Exception:
            logger.debug("Thumbnail encode failed", exc_info=True)
            return None
