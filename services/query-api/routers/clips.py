"""Clip proxy endpoints for the search UI.

Phase 5 shipped the standalone-clip path (`/clips/s3/{key:path}`) so the
search UI can play motion clips that landed in MinIO via Phase 4's
clip-service motion path.

Phase 9 adds `/clips/range` — server-side segment concatenation for
`range:cam:start|end` URIs emitted by Phase 4 for continuous-mode
recording. Downloads overlapping video_segments, concats with ffmpeg
(-c copy, no re-encode), trims to exact window, streams MP4.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from auth.jwt import get_current_user, require_role
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clips", tags=["clips"])

EVENT_CLIPS_BUCKET = "event-clips"

MAX_RANGE_SECONDS = 30 * 60
# Concurrent extractions capped — each uses ~500MB tmp + ffmpeg memory.
# Server has 15GB RAM and other services share the pool.
_EXTRACTION_SEMAPHORE = asyncio.Semaphore(2)


def _parse_storage_uri(storage_uri: str) -> tuple[str, str]:
    """s3://bucket/key/path -> (bucket, key/path)."""
    parsed = urlparse(storage_uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"invalid storage_uri: {storage_uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


@router.get(
    "/s3/{key:path}",
    dependencies=[require_role("admin", "operator", "viewer")],
)
async def fetch_s3_clip(
    key: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Stream a standalone clip from the MinIO event-clips bucket."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    if not key or ".." in key or key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid clip key")

    client = getattr(request.app.state, "minio_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Object storage unavailable")

    try:
        obj = client.get_object(EVENT_CLIPS_BUCKET, key)
    except Exception as exc:  # noqa: BLE001 — minio raises S3Error / others
        logger.warning("clip fetch failed bucket=%s key=%s: %s", EVENT_CLIPS_BUCKET, key, exc)
        raise HTTPException(status_code=404, detail="Clip not found") from exc

    def stream() -> Iterator[bytes]:
        try:
            for chunk in obj.stream(32 * 1024):
                yield chunk
        finally:
            obj.close()
            obj.release_conn()

    return StreamingResponse(stream(), media_type="video/mp4")


@router.get(
    "/range",
    dependencies=[require_role("admin", "operator", "viewer")],
)
async def extract_range(
    request: Request,
    camera_id: str = Query(..., min_length=1, max_length=64),
    start: datetime = Query(...),
    end: datetime = Query(...),
    user: UserClaims = Depends(get_current_user),
):
    """Concatenate video_segments covering [start, end] and stream MP4.

    Accepts segment_range URIs emitted by Phase 4. Parses bucket from
    each segment's storage_uri so hot/warm tiers work transparently.
    """
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    duration_s = (end - start).total_seconds()
    if duration_s <= 0:
        raise HTTPException(400, "end must be after start")
    if duration_s > MAX_RANGE_SECONDS:
        raise HTTPException(
            400,
            f"Range exceeds {MAX_RANGE_SECONDS // 60} minutes maximum. "
            "Use a narrower window.",
        )

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT segment_id, storage_uri, start_time, end_time, tier
            FROM video_segments
            WHERE camera_id = $1
              AND tier IN ('hot', 'warm')
              AND start_time < $3 AND end_time > $2
            ORDER BY start_time
            """,
            camera_id, start, end,
        )

    if not rows:
        async with pool.acquire() as conn:
            cold_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM video_segments
                    WHERE camera_id = $1 AND tier = 'cold'
                      AND start_time < $3 AND end_time > $2
                )
                """,
                camera_id, start, end,
            )
        if cold_exists:
            raise HTTPException(
                410,
                "Segments exist but have aged into cold tier (archived). "
                "Cold-tier playback not yet supported.",
            )
        raise HTTPException(
            404,
            "No video segments exist for this time range. "
            "Recorder may have been down, or retention has expired.",
        )

    minio_client = getattr(request.app.state, "minio_client", None)
    if minio_client is None:
        raise HTTPException(503, "Object storage unavailable")

    async with _EXTRACTION_SEMAPHORE:
        try:
            output_path, cleanup = await _extract_and_concat(
                rows=rows,
                minio_client=minio_client,
                start=start,
                end=end,
                camera_id=camera_id,
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "range extraction failed camera=%s start=%s end=%s",
                camera_id, start, end,
            )
            raise HTTPException(500, f"Extraction failed: {exc}") from exc

    def stream_and_cleanup() -> Iterator[bytes]:
        try:
            with open(output_path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            cleanup()

    safe_start = start.isoformat().replace(":", "-")
    filename = f"{camera_id}-{safe_start}.mp4"
    return StreamingResponse(
        stream_and_cleanup(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


async def _extract_and_concat(
    rows: list,
    minio_client,
    start: datetime,
    end: datetime,
    camera_id: str,
) -> tuple[Path, Callable[[], None]]:
    """Download segments, concat with ffmpeg, return (output_path, cleanup_fn)."""
    tmp_dir_obj = tempfile.TemporaryDirectory(prefix="cliprange-")
    tmp_path = Path(tmp_dir_obj.name)

    def cleanup() -> None:
        tmp_dir_obj.cleanup()

    try:
        segment_files: list[Path] = []
        for i, r in enumerate(rows):
            bucket, key = _parse_storage_uri(r["storage_uri"])
            local = tmp_path / f"seg-{i:03d}.ts"
            await asyncio.to_thread(
                _download_minio_object, minio_client, bucket, key, local,
            )
            segment_files.append(local)

        manifest = tmp_path / "list.txt"
        manifest.write_text("\n".join(f"file '{s.name}'" for s in segment_files))

        first_seg_start = rows[0]["start_time"]
        t_offset = max(0.0, (start - first_seg_start).total_seconds())
        window_duration = (end - start).total_seconds()

        output_path = tmp_path / "out.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "error",
            "-threads", "2",
            "-f", "concat", "-safe", "0",
            "-i", str(manifest),
            "-ss", str(t_offset),
            "-t", str(window_duration),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(tmp_path),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
            raise HTTPException(
                500,
                f"ffmpeg concat failed (exit {proc.returncode}): {stderr}",
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise HTTPException(500, "ffmpeg produced no output")

        return output_path, cleanup

    except Exception:
        cleanup()
        raise


def _download_minio_object(minio_client, bucket: str, key: str, local: Path) -> None:
    """Synchronous MinIO download (called via asyncio.to_thread)."""
    data = minio_client.get_object(bucket, key)
    try:
        with local.open("wb") as f:
            for chunk in data.stream(64 * 1024):
                f.write(chunk)
    finally:
        data.close()
        data.release_conn()
