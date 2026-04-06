"""Disk-backed ring buffer for NATS messages during connectivity outages.

Messages are stored as individual files in a directory, named by sequence
number.  When the buffer exceeds ``max_bytes`` the oldest files are deleted
(ring-buffer semantics).  On NATS recovery the ``drain()`` method replays
buffered messages in FIFO order with an optional rate limit.

File format per message::

    [subject_len: uint16][payload_len: uint32][subject_bytes][payload_bytes]
"""

from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path
from typing import Awaitable, Callable

from metrics import BUFFER_FILL

logger = logging.getLogger(__name__)

_HDR = struct.Struct("!HI")  # subject_len (uint16), payload_len (uint32)


class LocalBuffer:
    """Persistent ring buffer for (subject, payload) pairs."""

    def __init__(
        self,
        path: str | Path,
        max_bytes: int = 10 * 1024 * 1024 * 1024,
        replay_rate_limit: int = 100,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._replay_rate_limit = replay_rate_limit
        self._seq: int = 0
        self._current_bytes: int = 0
        self._path.mkdir(parents=True, exist_ok=True)
        self._scan_existing()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue(self, subject: str, payload: bytes) -> None:
        """Persist a message to disk."""
        subj_bytes = subject.encode("utf-8")
        hdr = _HDR.pack(len(subj_bytes), len(payload))
        data = hdr + subj_bytes + payload

        filepath = self._path / f"{self._seq:012d}.msg"
        await asyncio.to_thread(filepath.write_bytes, data)
        self._seq += 1
        self._current_bytes += len(data)
        BUFFER_FILL.set(self._current_bytes)

        await self._evict_if_needed()

    async def drain(
        self,
        publish_fn: Callable[[str, bytes], Awaitable[bool]],
    ) -> int:
        """Replay buffered messages in FIFO order.

        *publish_fn(subject, payload)* must return ``True`` on success.
        Replay stops on the first failure so messages remain in order.
        Returns the number of successfully replayed messages.
        """
        files = sorted(self._path.glob("*.msg"))
        if not files:
            return 0

        interval = (
            1.0 / self._replay_rate_limit if self._replay_rate_limit > 0 else 0
        )
        count = 0

        for fpath in files:
            data = await asyncio.to_thread(fpath.read_bytes)
            subject, payload = _decode_message(data)
            success = await publish_fn(subject, payload)
            if not success:
                break
            await asyncio.to_thread(fpath.unlink)
            self._current_bytes -= len(data)
            count += 1
            if interval:
                await asyncio.sleep(interval)

        BUFFER_FILL.set(max(self._current_bytes, 0))
        if count:
            logger.info("Drained %d buffered messages", count)
        return count

    @property
    def fill_bytes(self) -> int:
        return self._current_bytes

    @property
    def is_empty(self) -> bool:
        return self._current_bytes <= 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _scan_existing(self) -> None:
        """Recover sequence counter and byte total from existing files."""
        total = 0
        max_seq = -1
        for fpath in self._path.glob("*.msg"):
            total += fpath.stat().st_size
            try:
                seq = int(fpath.stem)
                max_seq = max(max_seq, seq)
            except ValueError:
                pass
        self._current_bytes = total
        self._seq = max_seq + 1 if max_seq >= 0 else 0
        BUFFER_FILL.set(self._current_bytes)

    async def _evict_if_needed(self) -> None:
        """Delete oldest messages until within budget."""
        if self._current_bytes <= self._max_bytes:
            return
        files = sorted(self._path.glob("*.msg"))
        while self._current_bytes > self._max_bytes and files:
            oldest = files.pop(0)
            size = oldest.stat().st_size
            await asyncio.to_thread(oldest.unlink)
            self._current_bytes -= size
            logger.debug("Evicted %s (%d bytes)", oldest.name, size)
        BUFFER_FILL.set(max(self._current_bytes, 0))


def _decode_message(data: bytes) -> tuple[str, bytes]:
    """Decode a stored message file into ``(subject, payload)``."""
    subj_len, payload_len = _HDR.unpack_from(data)
    offset = _HDR.size
    subject = data[offset : offset + subj_len].decode("utf-8")
    payload = data[offset + subj_len : offset + subj_len + payload_len]
    return subject, payload
