"""MTMC Re-ID Association Service.

Kafka consumer pipeline that performs cross-camera Re-ID matching:

1. Consume Embedding protos from ``mtmc.active_embeddings``
2. Look up local track metadata (camera, class) from DB
3. Index embedding in FAISS, search for cross-camera matches
4. Score candidates: topology filter, transit-time likelihood, attributes
5. Persist matches as global tracks / global track links
6. Checkpoint FAISS index periodically (local + MinIO)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np

from checkpoint import CheckpointData, CheckpointManager
from config import MTMCSettings
from faiss_index import FAISSIndex
from matcher import Matcher
from metrics import (
    EMBEDDINGS_CONSUMED,
    FAISS_INDEX_SIZE,
    REBALANCE_DURATION,
    REJECTS_TOTAL,
)
from publisher import DBWriter
from topology_client import TopologyClient

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


def _load_embedding_type() -> type[Any]:
    try:
        from vidanalytics.v1.embedding import embedding_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return embedding_pb2.Embedding


class MTMCService:
    """Main MTMC Re-ID service orchestrator."""

    def __init__(self, settings: MTMCSettings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self._index: FAISSIndex | None = None
        self._checkpoint_mgr: CheckpointManager | None = None
        self._topo_client: TopologyClient | None = None
        self._matcher: Matcher | None = None
        self._db_writer: DBWriter | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._started_at: float = time.time()
        self._consumer_subscribed: bool = False
        self._health_runner: Any = None

    async def start(self) -> None:
        """Initialise all subsystems and start the consumer loop."""
        # DB connection pool
        self._pool = await asyncpg.create_pool(
            dsn=self.settings.db_dsn, min_size=2, max_size=10,
        )
        self._db_writer = DBWriter(self._pool)

        # Topology
        self._topo_client = TopologyClient(
            self._pool,
            self.settings.site_id,
            self.settings.topology_refresh_s,
        )
        await self._topo_client.load()

        # FAISS index
        self._index = FAISSIndex(
            dimension=512,
            active_horizon_minutes=self.settings.active_horizon_minutes,
        )

        # Checkpoint manager
        minio_client = self._create_minio()
        self._checkpoint_mgr = CheckpointManager(
            local_path=self.settings.checkpoint_local_path,
            minio_client=minio_client,
            minio_bucket=self.settings.minio_bucket,
            site_id=self.settings.site_id,
            local_interval_s=self.settings.checkpoint_local_interval_s,
            minio_interval_s=self.settings.checkpoint_minio_interval_s,
        )

        # Restore from checkpoint
        await self._restore_checkpoint()

        # Matcher
        self._matcher = Matcher(
            faiss_index=self._index,
            topology_client=self._topo_client,
            db_writer=self._db_writer,
            site_id=self.settings.site_id,
            faiss_k=self.settings.faiss_k,
            match_threshold=self.settings.match_threshold,
            active_horizon_minutes=self.settings.active_horizon_minutes,
            weight_cosine=self.settings.score_weight_cosine,
            weight_transit=self.settings.score_weight_transit,
            weight_attribute=self.settings.score_weight_attribute,
        )

        # Prometheus metrics server
        from prometheus_client import start_http_server  # noqa: PLC0415
        start_http_server(self.settings.metrics_port)
        logger.info("Metrics server on port %d", self.settings.metrics_port)

        # Health server
        await self._start_health_server()

        # Background tasks
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        # Start consuming
        await self._consume_loop()

    async def _start_health_server(self) -> None:
        try:
            from aiohttp import web  # noqa: PLC0415
        except ImportError:
            logger.warning("aiohttp not installed — /health endpoint disabled")
            return

        async def health_handler(_request: Any) -> Any:
            now = time.time()
            uptime = now - self._started_at
            checks: dict[str, str] = {}
            healthy = True

            if self._consumer_subscribed:
                checks["consumer"] = "connected"
            else:
                checks["consumer"] = "disconnected"
                healthy = False

            if self._pool is not None:
                try:
                    async with self._pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                    checks["database"] = "connected"
                except Exception as exc:
                    checks["database"] = f"error: {type(exc).__name__}"
                    healthy = False
            else:
                checks["database"] = "not initialised"
                healthy = False

            if self._index is not None:
                checks["faiss_size"] = str(self._index.size())
            else:
                checks["faiss_size"] = "0"

            body = {
                "status": "ok" if healthy else "unhealthy",
                "uptime_seconds": int(uptime),
                "checks": checks,
            }
            return web.json_response(body, status=200 if healthy else 503)

        app = web.Application()
        app.router.add_get("/health", health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.health_port)
        await site.start()
        self._health_runner = runner
        logger.info("Health server on port %d", self.settings.health_port)

    async def shutdown(self) -> None:
        """Graceful shutdown: flush checkpoint, close connections."""
        self._shutdown.set()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()

        # Final checkpoint
        if self._index is not None and self._checkpoint_mgr is not None:
            await self._do_checkpoint(force=True)

        if self._health_runner is not None:
            try:
                await self._health_runner.cleanup()
            except Exception:
                pass

        if self._pool is not None:
            await self._pool.close()
        logger.info("MTMC service shut down")

    # ------------------------------------------------------------------
    # Kafka consumer loop (confluent-kafka)
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        from confluent_kafka import Consumer, KafkaError  # noqa: PLC0415

        cfg = self.settings
        consumer_config: dict[str, Any] = {
            "bootstrap.servers": cfg.kafka_bootstrap,
            "group.id": cfg.kafka_group_id,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "partition.assignment.strategy": "cooperative-sticky",
        }
        if cfg.kafka_security_protocol != "PLAINTEXT":
            consumer_config["security.protocol"] = cfg.kafka_security_protocol

        consumer = Consumer(consumer_config)

        rebalance_start = 0.0

        def on_assign(c: Any, partitions: Any) -> None:
            nonlocal rebalance_start
            if rebalance_start > 0:
                REBALANCE_DURATION.observe(time.time() - rebalance_start)
            logger.info("Partitions assigned: %s", partitions)

        def on_revoke(c: Any, partitions: Any) -> None:
            nonlocal rebalance_start
            rebalance_start = time.time()
            logger.info("Partitions revoked: %s", partitions)

        consumer.subscribe(
            [cfg.kafka_topic],
            on_assign=on_assign,
            on_revoke=on_revoke,
        )
        self._consumer_subscribed = True

        EmbeddingType = _load_embedding_type()
        logger.info(
            "Consuming from %s (group=%s)", cfg.kafka_topic, cfg.kafka_group_id
        )

        try:
            while not self._shutdown.is_set():
                # Poll in a thread to avoid blocking the event loop
                msg = await asyncio.to_thread(
                    consumer.poll, cfg.kafka_poll_timeout_s
                )

                if msg is None:
                    # No message — run maintenance
                    await self._maybe_checkpoint()
                    await self._maybe_refresh_topology()
                    continue

                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("Kafka error: %s", msg.error())
                    continue

                EMBEDDINGS_CONSUMED.inc()

                # Handle tombstone (null value = track terminated)
                if msg.value() is None:
                    key = msg.key()
                    if key is not None:
                        track_id = key.decode("utf-8")
                        self._index.remove_by_track(track_id)
                        logger.debug("Tombstone: removed track %s", track_id)
                    await asyncio.to_thread(consumer.commit, asynchronous=False)
                    continue

                # Deserialise Embedding proto
                try:
                    emb = EmbeddingType()
                    emb.ParseFromString(msg.value())
                except Exception:
                    logger.warning(
                        "Failed to parse Embedding proto at offset %d",
                        msg.offset(),
                        exc_info=True,
                    )
                    REJECTS_TOTAL.labels(
                        site_id=cfg.site_id, reason="parse_error"
                    ).inc()
                    await asyncio.to_thread(consumer.commit, asynchronous=False)
                    continue

                # Extract fields
                vector = np.array(emb.vector, dtype=np.float32)
                if len(vector) != 512:
                    logger.warning(
                        "Unexpected embedding dimension %d, expected 512",
                        len(vector),
                    )
                    await asyncio.to_thread(consumer.commit, asynchronous=False)
                    continue

                # Use edge_receive_ts as the primary timestamp
                timestamp = time.time()  # fallback: current time

                # Process through matcher
                try:
                    await self._matcher.process_embedding(
                        embedding_id=emb.embedding_id,
                        local_track_id=emb.source_id,
                        vector=vector,
                        model_version=emb.model_version,
                        quality_score=emb.quality_score,
                        timestamp=timestamp,
                    )
                except Exception:
                    logger.exception(
                        "Error processing embedding %s", emb.embedding_id
                    )

                # Update metrics
                FAISS_INDEX_SIZE.set(self._index.size())

                # Commit
                await asyncio.to_thread(consumer.commit, asynchronous=False)

                # Periodic maintenance
                await self._maybe_checkpoint()
                await self._maybe_refresh_topology()

        finally:
            self._consumer_subscribed = False
            consumer.close()

    # ------------------------------------------------------------------
    # Checkpoint management
    # ------------------------------------------------------------------

    async def _restore_checkpoint(self) -> None:
        """Restore FAISS index from checkpoint (MinIO -> local -> empty)."""
        assert self._checkpoint_mgr is not None
        assert self._index is not None

        data = await asyncio.to_thread(self._checkpoint_mgr.restore)
        if data is not None:
            self._index.restore_state(
                data.index, data.metadata, data.id_map,
                data.track_map, data.next_id,
            )
            FAISS_INDEX_SIZE.set(self._index.size())
            logger.info("Restored %d embeddings from checkpoint", self._index.size())

    async def _maybe_checkpoint(self) -> None:
        assert self._checkpoint_mgr is not None
        assert self._index is not None

        if self._checkpoint_mgr.should_save_local():
            await self._do_checkpoint(force=False)
        elif self._checkpoint_mgr.should_save_minio():
            await self._do_checkpoint(force=False)

        self._checkpoint_mgr.update_lag_metric()

    async def _do_checkpoint(self, force: bool = False) -> None:
        assert self._checkpoint_mgr is not None
        assert self._index is not None

        state = self._index.get_state()
        data = CheckpointData(
            index=state[0],
            metadata=state[1],
            id_map=state[2],
            track_map=state[3],
            next_id=state[4],
            embedding_count=self._index.size(),
        )

        if force or self._checkpoint_mgr.should_save_local():
            await asyncio.to_thread(self._checkpoint_mgr.save_local, data)

        if force or self._checkpoint_mgr.should_save_minio():
            await asyncio.to_thread(self._checkpoint_mgr.save_minio, data)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def _maybe_refresh_topology(self) -> None:
        if self._topo_client is not None:
            await self._topo_client.maybe_refresh()

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired embeddings from the FAISS index."""
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(60)
                if self._index is not None:
                    removed = await asyncio.to_thread(self._index.cleanup_expired)
                    if removed > 0:
                        FAISS_INDEX_SIZE.set(self._index.size())
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic cleanup")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_minio(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError:
            logger.warning("minio package not installed — checkpoints disabled")
            return None

        return Minio(
            self.settings.minio_url,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=self.settings.minio_secure,
        )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


async def run(settings: MTMCSettings) -> None:
    service = MTMCService(settings)
    loop = asyncio.get_event_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        asyncio.ensure_future(service.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await service.start()
    except asyncio.CancelledError:
        pass
    finally:
        await service.shutdown()


def main() -> None:
    args = parse_args()
    settings = MTMCSettings.from_yaml(args.config)
    setup_logging(settings.log_level)
    logger.info("Starting MTMC Re-ID service (site=%s)", settings.site_id)
    asyncio.run(run(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
