"""FFmpeg subprocess wrapper for event clip extraction."""

from __future__ import annotations

import asyncio
from pathlib import Path


async def extract_clip(
    frame_paths: list[Path],
    output_path: Path,
    target_bitrate: str = "1500k",
    fps: int = 5,
) -> Path:
    """Encode ordered JPEG frames into an H.264 baseline MP4 clip."""
    if not frame_paths:
        raise ValueError("cannot extract clip from an empty frame list")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path = output_path.parent / f"{output_path.stem}_concat.txt"
    concat_path.write_text(_build_concat_file(frame_paths), encoding="utf-8")

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-b:v",
        target_bitrate,
        "-r",
        str(fps),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    try:
        await _run_ffmpeg(args)
    finally:
        concat_path.unlink(missing_ok=True)

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg completed but did not produce a non-empty clip")
    return output_path


def _build_concat_file(frame_paths: list[Path]) -> str:
    """Build an FFmpeg concat manifest for the input JPEG frames."""
    return "".join(f"file '{_escape_concat_path(path)}'\n" for path in frame_paths)


def _escape_concat_path(path: Path) -> str:
    return path.as_posix().replace("'", "'\\''")


async def _run_ffmpeg(args: list[str]) -> None:
    """Execute FFmpeg and raise on non-zero exit."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed with exit code {process.returncode}: {error_text}")
