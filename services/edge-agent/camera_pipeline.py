"""Per-camera pipeline: RTSP decode -> motion filter -> NATS publish.

Each ``CameraPipeline`` instance manages one camera's lifecycle:

1. Connect to camera via RTSP (GStreamer)
2. Pull decoded frames
3. Stamp ``edge_receive_ts`` **immediately** on arrival (trust anchor)
4. Run motion detection — forward ~15% of frames
5. Encode passing frame as JPEG, upload to MinIO
6. Publish ``FrameRef`` protobuf to NATS JetStream
7. On NATS failure: buffer to disk, drain on reconnect
8. On RTSP failure: reconnect with exponential backoff
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from datetime import datetime, timezone

import numpy as np
from PIL import Image

from config import CameraConfig, MinioConfig, MotionConfig
from local_buffer import LocalBuffer
from metrics import CAMERA_UPTIME, MOTION_FRAMES, STATIC_FILTERED
from motion_detector import MotionDetector
from nats_publisher import NatsPublisher, serialize_frame_ref
from rtsp_client import RtspClient

logger = logging.getLogger(__name__)


class CameraPipeline:
    """Runs the full capture-filter-publish loop for a single camera."""

    def __init__(
        self,
        camera: CameraConfig,
        site_id: str,
        nats_pub: NatsPublisher,
        minio_client: object,  # minio.Minio
        minio_cfg: MinioConfig,
        motion_cfg: MotionConfig,
        buffer: LocalBuffer,
    ) -> None:
        self._camera = camera
        self._site_id = site_id
        self._nats = nats_pub
        self._minio = minio_client
        self._minio_cfg = minio_cfg
        self._buffer = buffer
        self._shutdown = False

        self._rtsp = RtspClient(camera.camera_id, camera.rtsp_url)
        self._motion = MotionDetector(
            pixel_threshold=motion_cfg.pixel_threshold,
            motion_threshold=motion_cfg.motion_threshold,
            scene_change_threshold=motion_cfg.scene_change_threshold,
            reference_update_interval_s=motion_cfg.reference_update_interval_s,
        )

    async def run(self) -> None:
        """Main loop — runs until ``shutdown()`` is called."""
        cam_id = self._camera.camera_id
        while not self._shutdown:
            try:
                await self._rtsp.start()
                self._rtsp.reset_backoff()
                await self._drain_buffer()
                await self._capture_loop()
            except Exception:
                logger.exception("Pipeline error for %s", cam_id)
            finally:
                CAMERA_UPTIME.labels(camera_id=cam_id).set(
                    self._rtsp.uptime_ratio
                )

            if not self._shutdown:
                try:
                    await self._rtsp.reconnect_with_backoff()
                except Exception:
                    logger.exception("Reconnect failed for %s", cam_id)
                    await asyncio.sleep(self._rtsp.MAX_BACKOFF_S)

    def shutdown(self) -> None:
        self._shutdown = True

    # ------------------------------------------------------------------
    # Core capture loop
    # ------------------------------------------------------------------

    async def _capture_loop(self) -> None:
        cam_id = self._camera.camera_id

        while not self._shutdown:
            frame = await self._rtsp.read_frame()
            if frame is None:
                logger.warning(
                    "No frame from %s — stream may have ended", cam_id
                )
                return  # triggers reconnect in run()

            # --- edge_receive_ts stamped IMMEDIATELY on arrival ---
            edge_receive_ts = time.time()

            has_motion, _is_scene_change = self._motion.detect(frame.data)
            if not has_motion:
                STATIC_FILTERED.labels(camera_id=cam_id).inc()
                continue

            MOTION_FRAMES.labels(camera_id=cam_id).inc()

            # Upload frame to MinIO
            frame_id = str(uuid.uuid4())
            try:
                frame_uri = await self._upload_frame(
                    frame.data, frame_id, cam_id
                )
            except Exception:
                logger.warning(
                    "MinIO upload failed for %s — dropping frame", cam_id,
                    exc_info=True,
                )
                continue

            # source_capture_ts: best-effort from PTS (advisory / untrusted)
            source_capture_ts = _pts_to_epoch(frame.pts_ns, edge_receive_ts)

            payload = serialize_frame_ref(
                frame_id=frame_id,
                camera_id=cam_id,
                frame_uri=frame_uri,
                frame_sequence=frame.sequence,
                source_capture_ts=source_capture_ts,
                edge_receive_ts=edge_receive_ts,
                width_px=frame.width,
                height_px=frame.height,
                codec="jpeg",
                clock_quality=3,  # CLOCK_QUALITY_ESTIMATED
            )

            subject = self._nats.live_subject(cam_id)
            ok = await self._nats.publish(subject, payload, camera_id=cam_id)
            if not ok:
                await self._buffer.enqueue(subject, payload)

            CAMERA_UPTIME.labels(camera_id=cam_id).set(
                self._rtsp.uptime_ratio
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _drain_buffer(self) -> None:
        """Drain any buffered messages from a prior NATS outage."""
        if self._buffer.is_empty or not self._nats.is_connected:
            return

        async def _pub(subject: str, payload: bytes) -> bool:
            return await self._nats.publish(subject, payload)

        await self._buffer.drain(_pub)

    async def _upload_frame(
        self, data: np.ndarray, frame_id: str, camera_id: str
    ) -> str:
        """Encode as JPEG, upload to MinIO, return the ``s3://`` URI."""
        img = Image.fromarray(data)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        object_name = f"{camera_id}/{date_str}/{frame_id}.jpg"
        bucket = self._minio_cfg.bucket

        await asyncio.to_thread(
            self._minio.put_object,  # type: ignore[union-attr]
            bucket,
            object_name,
            buf,
            buf.getbuffer().nbytes,
            "image/jpeg",
        )
        return f"s3://{bucket}/{object_name}"


def _pts_to_epoch(pts_ns: int, fallback_epoch: float) -> float:
    """Best-effort PTS-to-epoch conversion.

    GStreamer RTSP PTS values are stream-relative (not wall-clock).
    Without RTCP SR correlation we fall back to ``edge_receive_ts``.
    The timestamp is marked ``CLOCK_QUALITY_ESTIMATED`` in the proto.
    """
    # GST_CLOCK_TIME_NONE = 2^64 - 1
    if pts_ns <= 0 or pts_ns >= (1 << 63):
        return fallback_epoch
    return fallback_epoch
