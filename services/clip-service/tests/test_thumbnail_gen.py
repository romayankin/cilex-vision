"""Tests for thumbnail generation."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from thumbnail_gen import generate_thumbnail


def _write_frame(path: Path, rgb: tuple[int, int, int]) -> None:
    Image.new("RGB", (640, 360), rgb).save(path, format="JPEG", quality=90)


@pytest.mark.asyncio
async def test_generate_thumbnail_creates_resized_jpeg(tmp_path: Path) -> None:
    frame_path = tmp_path / "frame.jpg"
    _write_frame(frame_path, (10, 20, 30))

    output_path = tmp_path / "thumb.jpg"
    result = await generate_thumbnail([frame_path], output_path, width=320, height=180)

    assert result == output_path
    assert output_path.exists()
    with Image.open(output_path) as thumb:
        assert thumb.size == (320, 180)


@pytest.mark.asyncio
async def test_generate_thumbnail_uses_middle_frame(tmp_path: Path) -> None:
    frame_paths = []
    for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255))):
        frame_path = tmp_path / f"frame-{index}.jpg"
        _write_frame(frame_path, color)
        frame_paths.append(frame_path)

    output_path = tmp_path / "thumb.jpg"
    await generate_thumbnail(frame_paths, output_path)

    with Image.open(output_path) as thumb:
        pixel = thumb.getpixel((thumb.width // 2, thumb.height // 2))
        assert pixel[1] > pixel[0]
        assert pixel[1] > pixel[2]


@pytest.mark.asyncio
async def test_generate_thumbnail_rejects_empty_frame_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty frame list"):
        await generate_thumbnail([], tmp_path / "thumb.jpg")
