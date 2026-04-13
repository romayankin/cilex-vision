"""Detection & Tracking Inference Worker.

Kafka consumer pipeline:

1. Consume ``FrameRef`` from ``frames.decoded.refs``
2. Download frame JPEG from MinIO
3. Detect objects via Triton YOLOv8-L
4. Track per camera via ByteTrack (CPU)
5. Extract Re-ID embeddings via Triton OSNet (best frame per track)
6. Publish: Detections → ``bulk.detections``,
   Tracklets → ``tracklets.local``,
   Embeddings → ``mtmc.active_embeddings``
7. Debug trace: sample 1–5% of paths to MinIO
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
from pathlib import Path
from typing import Any

import numpy as np

from config import Settings
from debug_trace import TraceCollector, TraceStage
from detector_client import DetectorClient
from embedder_client import EmbedderClient
from metrics import (
    CONSUMER_LAG,
    FRAMES_CONSUMED,
    TRACKS_ACTIVE,
    TRACKS_CLOSED,
)
from publisher import KafkaPublisher
from tracker import ByteTracker, TrackState

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


class InferenceWorker:
    """Main inference pipeline orchestrator."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()

        if settings.triton.url:
            self._detector = DetectorClient(settings.triton, settings.detector)
            self._embedder = EmbedderClient(settings.triton)
        else:
            from detector_cpu import CpuDetectorClient  # noqa: PLC0415
            from embedder_cpu import CpuEmbedderClient  # noqa: PLC0415

            logger.info("Triton URL not set — using CPU inference mode")
            self._detector = CpuDetectorClient(
                confidence_threshold=settings.detector.confidence_threshold,
                nms_iou_threshold=settings.detector.nms_iou_threshold,
            )
            self._embedder = CpuEmbedderClient()
        self._publisher = KafkaPublisher(settings.kafka)
        self._trackers: dict[str, ByteTracker] = {}

        # MinIO client for frame download and debug traces
        self._minio = None  # lazy init
        self._trace_collector = None  # lazy init

    async def start(self) -> None:
        """Connect to Kafka, MinIO, and start the consumer loop."""
        self._minio = self._create_minio()
        if self.settings.debug.enabled:
            self._trace_collector = TraceCollector(
                sample_rate=self.settings.debug.sample_rate_pct / 100.0,
                low_confidence_threshold=self.settings.debug.low_confidence_threshold,
                minio_client=self._minio,
                bucket=self.settings.minio.debug_bucket,
            )
            await self._trace_collector.ensure_bucket()

        await self._publisher.connect()
        logger.info("Kafka publisher connected")

        # Start Prometheus metrics server
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

                    # Commit after processing the batch
                    await consumer.commit()

                    # Update lag
                    try:
                        end_offsets = await consumer.end_offsets([partition])
                        position = await consumer.position(partition)
                        lag = max(int(end_offsets.get(partition, 0)) - int(position), 0)
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
        """Full detect → track → embed pipeline for one FrameRef."""
        FRAMES_CONSUMED.inc()

        FrameRef = load_frame_ref_type()
        frame_ref = FrameRef()
        frame_ref.ParseFromString(msg.value)

        camera_id = frame_ref.camera_id
        frame_id = frame_ref.frame_id
        frame_uri = frame_ref.frame_uri
        frame_sequence = int(frame_ref.frame_sequence)
        timestamps = frame_ref.timestamps

        # edge_receive_ts is the authoritative time reference
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
            source_ts = None

        # --- Debug trace: decide early ---
        should_trace, trace_reason = False, ""
        trace = None
        if self._trace_collector:
            should_trace, trace_reason = self._trace_collector.should_collect()

        # --- Download frame from MinIO ---
        t_download_start = time.monotonic()
        frame_data = await self._download_frame(frame_uri)
        t_download_end = time.monotonic()

        if frame_data is None:
            logger.warning("Failed to download frame %s", frame_uri)
            return

        if should_trace and self._trace_collector:
            trace = self._trace_collector.begin(
                frame_id, camera_id, frame_uri, trace_reason,
                kafka_offset=msg.offset,
                source_capture_ts=source_ts,
                edge_receive_ts=edge_ts,
                core_ingest_ts=time.time(),
            )
            trace.stages.append(
                TraceStage("download", t_download_start, t_download_end)
            )

        # --- Detect ---
        t_detect_start = time.monotonic()
        detections = await self._detector.detect(frame_data)
        t_detect_end = time.monotonic()

        if trace:
            trace.stages.append(
                TraceStage("detect", t_detect_start, t_detect_end)
            )
            self._trace_collector.collect_post_nms_detections(trace, detections)

        # Check for low-confidence detections to force trace
        if not should_trace and self._trace_collector:
            should_trace, trace_reason = self._trace_collector.should_collect(detections)
            if should_trace and trace is None:
                trace = self._trace_collector.begin(
                    frame_id, camera_id, frame_uri, trace_reason,
                    kafka_offset=msg.offset,
                    source_capture_ts=source_ts,
                    edge_receive_ts=edge_ts,
                    core_ingest_ts=time.time(),
                )
                trace.stages.append(
                    TraceStage("download", t_download_start, t_download_end)
                )
                trace.stages.append(
                    TraceStage("detect", t_detect_start, t_detect_end)
                )
                self._trace_collector.collect_post_nms_detections(trace, detections)

        # --- Track ---
        tracker = self._get_tracker(camera_id)
        active_before = tracker.active_track_count
        t_track_start = time.monotonic()
        updated_tracks, terminated_tracks = tracker.update(
            detections, edge_ts, frame_data
        )
        t_track_end = time.monotonic()

        if trace:
            trace.stages.append(
                TraceStage("track", t_track_start, t_track_end)
            )
            self._trace_collector.collect_tracker_delta(
                trace,
                active_before=active_before,
                active_after=tracker.active_track_count,
                new_track_ids=[
                    t.track_id for t in updated_tracks
                    if t.state == TrackState.NEW
                ],
                closed_track_ids=[
                    t.track_id for t in terminated_tracks
                ],
            )

        # Update metrics
        TRACKS_ACTIVE.labels(camera_id=camera_id).set(tracker.active_track_count)
        for _ in terminated_tracks:
            TRACKS_CLOSED.labels(camera_id=camera_id).inc()

        # Build detection→track assignment map for bulk-collector headers
        track_assignments: dict[int, str] = {}
        for i, det in enumerate(detections):
            for track in updated_tracks:
                if track.trajectory and abs(
                    track.trajectory[-1].centroid_x - (det.x_min + det.x_max) / 2
                ) < 0.001:
                    track_assignments[i] = track.track_id
                    break

        # --- Embed (best frame per active/new track) ---
        t_embed_start = time.monotonic()
        embeddings: dict[str, np.ndarray] = {}
        for track in updated_tracks:
            if (
                track.state in (TrackState.ACTIVE, TrackState.NEW)
                and track.best_frame_data is not None
                and track.best_confidence == track.confidence
            ):
                bbox = (
                    float(track.bbox[0]),
                    float(track.bbox[1]),
                    float(track.bbox[2]),
                    float(track.bbox[3]),
                )
                try:
                    emb = await self._embedder.extract(
                        track.best_frame_data, bbox
                    )
                    embeddings[track.track_id] = emb
                except Exception:
                    logger.debug(
                        "Embedding extraction failed for track %s",
                        track.track_id,
                        exc_info=True,
                    )
        t_embed_end = time.monotonic()

        if trace:
            trace.stages.append(
                TraceStage("embed", t_embed_start, t_embed_end)
            )
            trace.labels["embeddings_extracted"] = str(len(embeddings))

        # --- Publish ---
        t_pub_start = time.monotonic()

        # 1. Detection protos → bulk.detections
        await self._publisher.publish_detections(
            detections,
            frame_id,
            camera_id,
            frame_sequence,
            timestamps,
            track_assignments,
        )

        # 2. Tracklet protos → tracklets.local
        for track in updated_tracks:
            await self._publisher.publish_tracklet(track, timestamps)

        # 3. Embedding protos → mtmc.active_embeddings
        for track_id, emb in embeddings.items():
            track = next(
                (t for t in updated_tracks if t.track_id == track_id), None
            )
            if track:
                await self._publisher.publish_embedding(track, emb)

        # 4. Tombstones for terminated tracks
        for track in terminated_tracks:
            await self._publisher.publish_tracklet(track, timestamps)
            await self._publisher.publish_tombstone(track.track_id)

        t_pub_end = time.monotonic()

        if trace:
            trace.stages.append(
                TraceStage("publish", t_pub_start, t_pub_end)
            )
            self._trace_collector.set_model_versions(trace, {
                "detector": self.settings.triton.detector_model,
                "embedder": self.settings.triton.embedder_model,
            })

        # --- Store debug trace ---
        if trace and self._trace_collector:
            await self._trace_collector.store(trace)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_tracker(self, camera_id: str) -> ByteTracker:
        if camera_id not in self._trackers:
            self._trackers[camera_id] = ByteTracker(
                camera_id, self.settings.tracker
            )
        return self._trackers[camera_id]

    async def _download_frame(self, frame_uri: str) -> np.ndarray | None:
        """Download JPEG from MinIO and decode to RGB numpy array."""
        if self._minio is None:
            return None

        # Parse s3://bucket/path
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
        except Exception:
            logger.warning("MinIO download failed: %s", frame_uri, exc_info=True)
            return None

        from PIL import Image  # noqa: PLC0415

        img = Image.open(io.BytesIO(data)).convert("RGB")
        return np.array(img)

    def _create_minio(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError:
            logger.warning("minio package not installed — frame download disabled")
            return None

        cfg = self.settings.minio
        client = Minio(
            cfg.endpoint,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secure=cfg.secure,
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
    worker = InferenceWorker(settings)
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
    logger.info("Starting inference worker")
    asyncio.run(run(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
