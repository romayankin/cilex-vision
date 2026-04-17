"""Thumbnail generation from extracted source frames."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)


async def generate_thumbnail(
    frame_paths: list[Path],
    output_path: Path,
    width: int = 320,
    height: int = 180,
) -> Path:
    """Create a thumbnail from the middle frame in the ordered frame list."""
    if not frame_paths:
        raise ValueError("cannot generate thumbnail from an empty frame list")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    middle_frame = frame_paths[len(frame_paths) // 2]
    await asyncio.to_thread(
        _resize_and_save,
        middle_frame,
        output_path,
        width,
        height,
    )
    return output_path


def _resize_and_save(
    source_path: Path,
    output_path: Path,
    width: int,
    height: int,
) -> None:
    with Image.open(source_path) as image:
        resized = image.convert("RGB").resize(
            (width, height),
            Image.Resampling.LANCZOS,
        )
        resized.save(output_path, format="JPEG", quality=90)


async def generate_thumbnail_from_video(
    video_path: Path,
    output_path: Path,
    width: int = 320,
    height: int = 180,
) -> Path:
    """Extract a thumbnail from the middle of a video clip via ffmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    probe_args = [
        "ffprobe",
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(video_path),
    ]
    probe = await asyncio.create_subprocess_exec(
        *probe_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    try:
        duration = float(stdout.decode().strip())
    except (ValueError, AttributeError):
        duration = 5.0

    mid_point = max(0.0, duration / 2)

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{mid_point:.2f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", f"scale={width}:{height}",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg thumbnail extraction failed (exit {proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg produced no thumbnail output")

    return output_path
