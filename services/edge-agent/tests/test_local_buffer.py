"""Tests for local_buffer.LocalBuffer — enqueue, drain, eviction."""

from __future__ import annotations

import pytest

from local_buffer import LocalBuffer, _decode_message, _HDR


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def buf_dir(tmp_path):
    """Return a fresh temporary buffer directory."""
    return tmp_path / "buffer"


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------

class TestEnqueueDrain:
    """Basic enqueue / drain lifecycle."""

    @pytest.mark.asyncio
    async def test_enqueue_creates_files(self, buf_dir) -> None:
        buf = LocalBuffer(buf_dir, max_bytes=1024 * 1024)
        await buf.enqueue("test.subject", b"payload-1")
        await buf.enqueue("test.subject", b"payload-2")
        assert not buf.is_empty
        assert buf.fill_bytes > 0
        files = list(buf_dir.glob("*.msg"))
        assert len(files) == 2

    @pytest.mark.asyncio
    async def test_drain_replays_in_order(self, buf_dir) -> None:
        buf = LocalBuffer(buf_dir, max_bytes=1024 * 1024, replay_rate_limit=0)
        subjects = []
        payloads = []

        for i in range(5):
            await buf.enqueue(f"subj.{i}", f"data-{i}".encode())

        async def _pub(subject: str, payload: bytes) -> bool:
            subjects.append(subject)
            payloads.append(payload)
            return True

        count = await buf.drain(_pub)
        assert count == 5
        assert subjects == [f"subj.{i}" for i in range(5)]
        assert payloads == [f"data-{i}".encode() for i in range(5)]
        assert buf.is_empty

    @pytest.mark.asyncio
    async def test_drain_stops_on_failure(self, buf_dir) -> None:
        buf = LocalBuffer(buf_dir, max_bytes=1024 * 1024, replay_rate_limit=0)
        for i in range(5):
            await buf.enqueue(f"subj.{i}", b"x")

        call_count = 0

        async def _pub_fail_at_3(subject: str, payload: bytes) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count < 3  # fail on the 3rd call

        count = await buf.drain(_pub_fail_at_3)
        assert count == 2  # only 2 succeeded
        # 3 messages remain on disk
        remaining = list(buf_dir.glob("*.msg"))
        assert len(remaining) == 3

    @pytest.mark.asyncio
    async def test_drain_empty_buffer(self, buf_dir) -> None:
        buf = LocalBuffer(buf_dir, max_bytes=1024 * 1024, replay_rate_limit=0)

        async def _pub(s: str, p: bytes) -> bool:
            raise AssertionError("should not be called")

        count = await buf.drain(_pub)
        assert count == 0


class TestEviction:
    """Ring-buffer eviction when max_bytes is exceeded."""

    @pytest.mark.asyncio
    async def test_evicts_oldest(self, buf_dir) -> None:
        # Each message is ~9 bytes (6-byte header + 1-byte subject + 2-byte payload).
        # With max_bytes=50, eviction starts after the 6th message (54 > 50).
        buf = LocalBuffer(buf_dir, max_bytes=50, replay_rate_limit=0)

        for i in range(10):
            await buf.enqueue("s", f"p{i}".encode())

        # Buffer should have evicted the oldest messages.
        assert buf.fill_bytes <= 50
        remaining = sorted(buf_dir.glob("*.msg"))
        assert len(remaining) < 10
        # The remaining files should be the most recent ones.
        seqs = [int(f.stem) for f in remaining]
        assert seqs == sorted(seqs)
        assert seqs[-1] == 9  # last message is always kept


class TestCrashRecovery:
    """State recovery after process restart."""

    @pytest.mark.asyncio
    async def test_scan_existing_files(self, buf_dir) -> None:
        buf1 = LocalBuffer(buf_dir, max_bytes=1024 * 1024, replay_rate_limit=0)
        await buf1.enqueue("a.b", b"hello")
        await buf1.enqueue("a.c", b"world")
        saved_bytes = buf1.fill_bytes

        # Simulate restart: create a new LocalBuffer over the same dir.
        buf2 = LocalBuffer(buf_dir, max_bytes=1024 * 1024, replay_rate_limit=0)
        assert buf2.fill_bytes == saved_bytes
        assert not buf2.is_empty


class TestMessageCodec:
    """Low-level message encode / decode."""

    def test_roundtrip(self) -> None:
        subject = "frames.live.site-a.cam-01"
        payload = b"\x00\x01\x02binary-data"
        subj_bytes = subject.encode("utf-8")
        hdr = _HDR.pack(len(subj_bytes), len(payload))
        data = hdr + subj_bytes + payload

        decoded_subj, decoded_payload = _decode_message(data)
        assert decoded_subj == subject
        assert decoded_payload == payload
