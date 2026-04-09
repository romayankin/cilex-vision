"""Thumbnail generation from extracted source frames."""

from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image


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
