"""Tests for the FFmpeg clip extractor."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from clip_extractor import extract_clip


def _write_jpeg(path: Path, rgb: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (320, 180), rgb)
    image.save(path, format="JPEG", quality=90)


@pytest.mark.asyncio
async def test_extract_clip_creates_nonempty_mp4(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed in the test environment")

    frames = []
    for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255))):
        frame_path = tmp_path / f"frame-{index}.jpg"
        _write_jpeg(frame_path, color)
        frames.append(frame_path)

    output_path = tmp_path / "clip.mp4"
    result = await extract_clip(frames, output_path, fps=2)

    assert result == output_path
    assert output_path.exists()
    assert output_path.stat().st_size > 0


@pytest.mark.asyncio
async def test_extract_clip_calls_ffmpeg_with_expected_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_paths = [tmp_path / "frame-0.jpg", tmp_path / "frame-1.jpg"]
    for frame_path in frame_paths:
        _write_jpeg(frame_path, (127, 127, 127))

    output_path = tmp_path / "clip.mp4"
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            output_path.write_bytes(b"clip-bytes")
            return (b"", b"")

    async def fake_create_subprocess_exec(
        *args: str,
        stdout: Any = None,
        stderr: Any = None,
    ) -> FakeProcess:
        captured["args"] = list(args)
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        return FakeProcess()

    monkeypatch.setattr(
        "clip_extractor.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    await extract_clip(frame_paths, output_path, target_bitrate="900k", fps=7)

    args = captured["args"]
    assert args[:5] == ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    assert "-f" in args and "concat" in args
    assert "-profile:v" in args and "baseline" in args
    assert "-b:v" in args and "900k" in args
    assert "-r" in args and "7" in args
    assert args[-1] == str(output_path)


@pytest.mark.asyncio
async def test_extract_clip_raises_on_ffmpeg_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_path = tmp_path / "frame.jpg"
    _write_jpeg(frame_path, (255, 255, 255))

    class FakeProcess:
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"boom")

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(
        "clip_extractor.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        await extract_clip([frame_path], tmp_path / "clip.mp4")
