"""Extract a clip from MPEG-TS buffer segments using ffmpeg."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path


async def extract_clip_from_buffer(
    segments: list[Path],
    segment_start: datetime,
    clip_start: datetime,
    clip_end: datetime,
    output_path: Path,
    target_bitrate: str = "2000k",
) -> Path:
    """Concatenate buffer segments and extract the requested time range."""
    if not segments:
        raise ValueError("No segments provided")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if clip_start.tzinfo is None:
        clip_start = clip_start.replace(tzinfo=timezone.utc)
    if clip_end.tzinfo is None:
        clip_end = clip_end.replace(tzinfo=timezone.utc)
    if segment_start.tzinfo is None:
        segment_start = segment_start.replace(tzinfo=timezone.utc)

    seek_s = max(0.0, (clip_start - segment_start).total_seconds())
    duration_s = max(1.0, (clip_end - clip_start).total_seconds())

    concat_path = output_path.parent / f"{output_path.stem}_concat.txt"
    concat_content = "".join(f"file '{seg.as_posix()}'\n" for seg in segments)
    concat_path.write_text(concat_content, encoding="utf-8")

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_path),
        "-ss", f"{seek_s:.2f}",
        "-t", f"{duration_s:.2f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-profile:v", "baseline",
        "-b:v", target_bitrate,
        "-threads", "2",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"ffmpeg buffer extraction failed (exit {proc.returncode}): {error_text}"
            )
    finally:
        concat_path.unlink(missing_ok=True)

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg completed but produced no output")

    return output_path
