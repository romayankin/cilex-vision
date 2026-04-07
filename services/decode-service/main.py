"""Central Decode & Frame Sampling Service.

Kafka consumer pipeline:

1. Consume ``FrameRef`` from ``frames.sampled.refs``
2. Download encoded frame from MinIO
3. Decode to RGB (GStreamer for H.264/H.265, Pillow for JPEG)
4. Color-space normalize (BT.601 / BT.709)
5. Resize to inference resolution (1280x720)
6. FPS-based sampling (skip frames exceeding target rate)
7. Encode as JPEG, upload to ``decoded-frames`` bucket
8. Publish updated ``FrameRef`` to ``frames.decoded.refs``
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import signal
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from config import Settings
from decoder import FrameDecoder
from metrics import CONSUMER_LAG, FRAMES_CONSUMED, PUBLISH_ERRORS
from publisher import KafkaPublisher
from sampler import FrameSampler

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="YAML config path."
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_frame_ref_type() -> type[Any]:
    try:
        from vidanalytics.v1.frame import frame_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return frame_pb2.FrameRef


class DecodeWorker:
    """Main decode pipeline orchestrator."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()

        self._decoder = FrameDecoder(
            output_width=settings.decode.output_width,
            output_height=settings.decode.output_height,
            default_color_space=settings.decode.default_color_space,
        )
        self._sampler = FrameSampler(target_fps=settings.sampler.target_fps)
        self._publisher = KafkaPublisher(settings.kafka)
        self._minio = None  # lazy init

    async def start(self) -> None:
        """Connect to Kafka, MinIO, and start the consumer loop."""
        self._minio = self._create_minio()
        await self._publisher.connect()
        logger.info("Kafka publisher connected")

        from prometheus_client import start_http_server  # noqa: PLC0415

        start_http_server(self.settings.metrics_port)
        logger.info("Metrics server on port %d", self.settings.metrics_port)

        await self._consume_loop()

    async def shutdown(self) -> None:
        self._shutdown.set()
        await self._publisher.close()

    # ------------------------------------------------------------------
    # Kafka consumer loop
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        try:
            from aiokafka import AIOKafkaConsumer  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'aiokafka'; install requirements.txt"
            ) from exc

        cfg = self.settings.kafka
        ssl_context = self._build_ssl_context()

        consumer = AIOKafkaConsumer(
            cfg.input_topic,
            bootstrap_servers=cfg.bootstrap_servers,
            group_id=cfg.consumer_group,
            security_protocol=cfg.security_protocol,
            sasl_mechanism=cfg.sasl_mechanism,
            sasl_plain_username=cfg.sasl_username,
            sasl_plain_password=cfg.sasl_password,
            ssl_context=ssl_context,
            enable_auto_commit=False,
            auto_offset_reset=cfg.auto_offset_reset,
        )
        await consumer.start()
        logger.info(
            "Consuming from %s (group=%s)", cfg.input_topic, cfg.consumer_group
        )

        try:
            while not self._shutdown.is_set():
                batches = await consumer.getmany(
                    timeout_ms=cfg.poll_timeout_ms,
                    max_records=cfg.max_poll_records,
                )
                if not batches:
                    continue

                for partition, messages in batches.items():
                    for msg in messages:
                        try:
                            await self._process_message(msg)
                        except Exception:
                            logger.exception(
                                "Error processing message offset=%d", msg.offset
                            )

                    await consumer.commit()

                    # Update consumer lag metric
                    try:
                        end_offsets = await consumer.end_offsets([partition])
                        position = await consumer.position(partition)
                        lag = max(
                            int(end_offsets.get(partition, 0)) - int(position), 0
                        )
                        CONSUMER_LAG.labels(
                            topic=partition.topic,
                            partition=str(partition.partition),
                        ).set(lag)
                    except Exception:
                        pass
        finally:
            await consumer.stop()

    # ------------------------------------------------------------------
    # Per-message pipeline
    # ------------------------------------------------------------------

    async def _process_message(self, msg: Any) -> None:
        """Decode → sample → upload → publish for one FrameRef."""
        FRAMES_CONSUMED.inc()

        FrameRef = load_frame_ref_type()
        frame_ref = FrameRef()
        frame_ref.ParseFromString(msg.value)

        camera_id = frame_ref.camera_id
        frame_uri = frame_ref.frame_uri
        frame_sequence = int(frame_ref.frame_sequence)
        codec = frame_ref.codec or "jpeg"
        width = int(frame_ref.width_px) or 1920
        height = int(frame_ref.height_px) or 1080
        timestamps = frame_ref.timestamps

        # Extract timestamps
        edge_ts = (
            timestamps.edge_receive_ts.seconds
            + timestamps.edge_receive_ts.nanos / 1e9
        )
        if edge_ts <= 0:
            edge_ts = time.time()

        source_ts = (
            timestamps.source_capture_ts.seconds
            + timestamps.source_capture_ts.nanos / 1e9
        )
        if source_ts <= 0:
            source_ts = edge_ts

        # FPS-based sampling: skip if too fast for this camera
        if not self._sampler.should_sample(camera_id, edge_ts):
            return

        # Download encoded frame from MinIO
        data = await self._download_frame(frame_uri)
        if data is None:
            logger.warning("Failed to download frame %s", frame_uri)
            return

        # Decode to RGB at target resolution
        colorimetry = None
        frame = await self._decoder.decode(
            data, codec, width, height, colorimetry
        )

        # Stamp core_ingest_ts
        core_ingest_ts = time.time()

        # Encode decoded frame as JPEG and upload
        new_frame_id = str(uuid.uuid4())
        decoded_uri = await self._upload_decoded(frame, new_frame_id, camera_id)
        if decoded_uri is None:
            PUBLISH_ERRORS.inc()
            return

        # Publish decoded FrameRef
        await self._publisher.publish_frame_ref(
            frame_id=new_frame_id,
            camera_id=camera_id,
            frame_uri=decoded_uri,
            frame_sequence=frame_sequence,
            width=self.settings.decode.output_width,
            height=self.settings.decode.output_height,
            codec="jpeg",
            source_capture_ts=source_ts,
            edge_receive_ts=edge_ts,
            core_ingest_ts=core_ingest_ts,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _download_frame(self, frame_uri: str) -> bytes | None:
        """Download raw encoded frame bytes from MinIO."""
        if self._minio is None:
            return None

        if frame_uri.startswith("s3://"):
            uri = frame_uri[5:]
        else:
            uri = frame_uri
        parts = uri.split("/", 1)
        if len(parts) != 2:
            logger.warning("Invalid frame_uri: %s", frame_uri)
            return None

        bucket, object_name = parts
        try:
            response = await asyncio.to_thread(
                self._minio.get_object, bucket, object_name
            )
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except Exception:
            logger.warning("MinIO download failed: %s", frame_uri, exc_info=True)
            return None

    async def _upload_decoded(
        self, frame: np.ndarray, frame_id: str, camera_id: str
    ) -> str | None:
        """Encode decoded frame as JPEG and upload to MinIO decoded bucket."""
        if self._minio is None:
            return None

        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.settings.decode.jpeg_quality)
        buf.seek(0)

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        object_name = f"{camera_id}/{date_str}/{frame_id}.jpg"
        bucket = self.settings.minio.decoded_bucket

        try:
            await asyncio.to_thread(
                self._minio.put_object,
                bucket,
                object_name,
                buf,
                buf.getbuffer().nbytes,
                "image/jpeg",
            )
            return f"s3://{bucket}/{object_name}"
        except Exception:
            logger.warning(
                "MinIO upload failed for %s", object_name, exc_info=True
            )
            return None

    def _create_minio(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "minio package not installed — frame download disabled"
            )
            return None

        cfg = self.settings.minio
        client = Minio(
            cfg.endpoint,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secure=cfg.secure,
        )

        # Ensure decoded bucket exists
        try:
            if not client.bucket_exists(cfg.decoded_bucket):
                client.make_bucket(cfg.decoded_bucket)
        except Exception:
            logger.warning(
                "Cannot ensure decoded bucket: %s", cfg.decoded_bucket
            )

        return client

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cfg = self.settings.kafka
        if not any([cfg.ssl_ca_file, cfg.ssl_cert_file, cfg.ssl_key_file]):
            return None
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if cfg.ssl_ca_file:
            ctx.load_verify_locations(cfg.ssl_ca_file)
        if cfg.ssl_cert_file and cfg.ssl_key_file:
            ctx.load_cert_chain(cfg.ssl_cert_file, cfg.ssl_key_file)
        return ctx


async def run(settings: Settings) -> None:
    worker = DecodeWorker(settings)
    loop = asyncio.get_event_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        asyncio.ensure_future(worker.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await worker.start()
    except asyncio.CancelledError:
        pass
    finally:
        await worker.shutdown()


def main() -> None:
    args = parse_args()
    settings = Settings.from_yaml(args.config)
    setup_logging(settings.log_level)
    logger.info("Starting decode service")
    asyncio.run(run(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
