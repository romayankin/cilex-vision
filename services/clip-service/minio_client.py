"""MinIO helpers for source-frame lookup and clip asset upload."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path


@dataclass(frozen=True)
class SourceFrame:
    """A source JPEG object selected for a clip window."""

    object_name: str
    last_modified: datetime


class ClipMinioClient:
    """Small async wrapper over the synchronous MinIO SDK."""

    def __init__(
        self,
        *,
        url: str,
        access_key: str,
        secret_key: str,
        secure: bool,
        source_bucket: str,
        clip_bucket: str,
        thumbnail_bucket: str,
    ) -> None:
        self.source_bucket = source_bucket
        self.clip_bucket = clip_bucket
        self.thumbnail_bucket = thumbnail_bucket

        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'minio'; install requirements.txt"
            ) from exc

        self._client = Minio(
            url,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    async def ensure_buckets(self) -> None:
        """Create output buckets when missing."""
        await self._ensure_bucket(self.clip_bucket)
        await self._ensure_bucket(self.thumbnail_bucket)

    async def list_source_frames(
        self,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[SourceFrame]:
        """List decoded-frame objects in the time window.

        The current decoded-frame key format is `camera_id/YYYY-MM-DD/frame_id.jpg`,
        so time filtering must use object `last_modified` rather than the key.
        """
        frames: list[SourceFrame] = []
        for current_date in _date_range(start_time.date(), end_time.date()):
            prefix = f"{camera_id}/{current_date.isoformat()}/"
            objects = await asyncio.to_thread(
                lambda: list(
                    self._client.list_objects(
                        self.source_bucket,
                        prefix=prefix,
                        recursive=True,
                    )
                )
            )

            for obj in objects:
                last_modified = getattr(obj, "last_modified", None)
                object_name = getattr(obj, "object_name", None)
                if object_name is None or last_modified is None:
                    continue

                timestamp = _to_utc(last_modified)
                if start_time <= timestamp <= end_time:
                    frames.append(
                        SourceFrame(
                            object_name=object_name,
                            last_modified=timestamp,
                        )
                    )

        frames.sort(key=lambda frame: (frame.last_modified, frame.object_name))
        return frames

    async def download_frames(
        self,
        frames: list[SourceFrame],
        destination_dir: Path,
    ) -> list[Path]:
        """Download ordered frame objects into a temp directory."""
        destination_dir.mkdir(parents=True, exist_ok=True)
        output_paths: list[Path] = []
        for index, frame in enumerate(frames):
            destination = destination_dir / f"{index:06d}.jpg"
            await asyncio.to_thread(
                self._client.fget_object,
                self.source_bucket,
                frame.object_name,
                str(destination),
            )
            output_paths.append(destination)
        return output_paths

    async def upload_clip(
        self,
        local_path: Path,
        site_id: str | None,
        camera_id: str,
        event_id: str,
        asset_date: date,
    ) -> str:
        """Upload the extracted MP4 clip and return its S3 URI."""
        object_name = _build_asset_key(
            site_id=site_id,
            camera_id=camera_id,
            asset_date=asset_date,
            file_name=f"{event_id}.mp4",
        )
        await asyncio.to_thread(
            partial(
                self._client.fput_object,
                self.clip_bucket,
                object_name,
                str(local_path),
                content_type="video/mp4",
            )
        )
        return f"s3://{self.clip_bucket}/{object_name}"

    async def upload_thumbnail(
        self,
        local_path: Path,
        site_id: str | None,
        camera_id: str,
        event_id: str,
        asset_date: date,
    ) -> str:
        """Upload the generated thumbnail and return its S3 URI."""
        object_name = _build_asset_key(
            site_id=site_id,
            camera_id=camera_id,
            asset_date=asset_date,
            file_name=f"{event_id}_thumb.jpg",
        )
        await asyncio.to_thread(
            partial(
                self._client.fput_object,
                self.thumbnail_bucket,
                object_name,
                str(local_path),
                content_type="image/jpeg",
            )
        )
        return f"s3://{self.thumbnail_bucket}/{object_name}"

    async def _ensure_bucket(self, bucket_name: str) -> None:
        exists = await asyncio.to_thread(self._client.bucket_exists, bucket_name)
        if not exists:
            await asyncio.to_thread(self._client.make_bucket, bucket_name)


def _date_range(start_date: date, end_date: date) -> list[date]:
    output: list[date] = []
    current = start_date
    while current <= end_date:
        output.append(current)
        current += timedelta(days=1)
    return output


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_asset_key(
    *,
    site_id: str | None,
    camera_id: str,
    asset_date: date,
    file_name: str,
) -> str:
    prefix_parts = [camera_id, asset_date.isoformat(), file_name]
    if site_id:
        prefix_parts.insert(0, site_id)
    return "/".join(prefix_parts)
