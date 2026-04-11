# Glossary

Domain terminology for operators of the Cilex Vision multi-camera video analytics platform.

---

## Object Classes

The platform detects and tracks 7 object classes. All models, database records, and API responses use these exact lowercase names.

| Class | Description |
|-------|-------------|
| `person` | Human individual |
| `car` | Passenger vehicle (sedan, SUV, hatchback) |
| `truck` | Commercial vehicle, pickup, or freight vehicle |
| `bus` | Public transit or shuttle bus |
| `bicycle` | Bicycle (with or without rider) |
| `motorcycle` | Motorcycle or scooter |
| `animal` | Any animal (dog, cat, bird, etc.) |

---

## Pipeline Stages

The Cilex Vision processing pipeline moves data through these stages in order:

### Edge

| Term | Definition |
|------|------------|
| **Edge agent** | Python service running at each physical site. Connects to cameras via RTSP, applies motion filtering, and publishes frame references to NATS. |
| **Motion filter** | Frame-differencing algorithm in the edge agent that suppresses static frames. Passes approximately 15% of incoming frames. |
| **RTSP** | Real Time Streaming Protocol. The standard protocol used to receive video from IP cameras. |
| **ONVIF** | Open Network Video Interface Forum. Protocol for camera discovery and configuration. |
| **Local buffer** | Disk-backed buffer on the edge agent that stores frames when the NATS connection is unavailable. |

### Ingest

| Term | Definition |
|------|------------|
| **Ingress bridge** | Service that consumes frames from NATS (edge) and publishes them to Kafka (center). Includes schema validation, blob offload to MinIO, and an NVMe spool for Kafka outages. |
| **NVMe spool** | 50GB disk buffer in the ingress bridge. Fills when Kafka is unavailable; drains when Kafka recovers. |
| **Schema validation** | Protobuf validation of incoming messages against the Schema Registry. Messages that fail validation are dead-lettered. |
| **Dead-letter queue (DLQ)** | A `{topic}.dlq` Kafka topic where messages are sent after 3 failed processing attempts. |

### Inference

| Term | Definition |
|------|------------|
| **Decode service** | Consumes encoded frames from Kafka, decodes them (GStreamer/NVDEC), normalizes to 1280x720 RGB, and publishes decoded frame references. |
| **Inference worker** | Core processing service. Runs object detection (YOLOv8-L), single-camera tracking (ByteTrack), and Re-ID embedding extraction (OSNet) on each frame. |
| **Triton Inference Server** | NVIDIA model serving platform. Hosts all GPU models with dynamic batching. Runs in EXPLICIT mode (no auto-load). |
| **NMS (Non-Maximum Suppression)** | Post-processing step after object detection that removes duplicate overlapping bounding boxes, keeping only the highest-confidence detection for each object. |
| **Dynamic batching** | Triton feature that groups multiple inference requests into a single GPU batch for efficiency. |
| **EXPLICIT mode** | Triton configuration (ADR-005) where models must be explicitly loaded via API. Prevents accidental model swaps during deployment. |

### Tracking

| Term | Definition |
|------|------------|
| **ByteTrack** | Single-camera multi-object tracker. Uses two-stage association (high and low confidence detections), Kalman filter for motion prediction, and 30-frame patience before closing a track. CPU-only. |
| **Local track** | A continuous sequence of detections of the same object within a single camera. Has a `local_track_id`. |
| **Track patience** | Number of frames a tracker waits for a missing detection before closing the track (30 frames for ByteTrack). |
| **ID switch** | When the tracker incorrectly assigns a new track ID to an object that was already being tracked, or merges two different objects into one track. |

### MTMC (Multi-Target Multi-Camera)

| Term | Definition |
|------|------------|
| **MTMC** | Multi-Target Multi-Camera association. Links local tracks from different cameras into global tracks representing the same physical object across the entire site. |
| **Re-ID (Re-Identification)** | Using visual appearance embeddings to determine if an object seen in one camera is the same as an object seen in another camera. |
| **OSNet** | Omni-Scale Network. The Re-ID model that produces 512-dimensional L2-normalized embeddings for appearance matching. |
| **FAISS** | Facebook AI Similarity Search. In-memory vector index used for fast nearest-neighbor matching of Re-ID embeddings. Used for the 30-minute active matching horizon (<1ms lookups). |
| **pgvector** | PostgreSQL extension for vector similarity search. Used for 90-day historical Re-ID queries (50-100ms at 6.5M vectors). |
| **Global track** | A cross-camera identity linking local tracks from multiple cameras. Has a `global_track_id`. |
| **Match threshold** | Minimum combined score (0.65) for an embedding comparison to be accepted as a cross-camera match. |
| **Checkpoint** | Periodic snapshot of the FAISS in-memory index to disk or MinIO. Enables fast recovery after a restart. |
| **Model version boundary** | Rule (ADR-008) that embeddings from different OSNet versions must never be compared. A model cutover triggers a FAISS flush and tracker reset (~30s matching blackout). |

### Storage

| Term | Definition |
|------|------------|
| **Bulk collector** | Service that consumes detection/tracklet/event messages from Kafka and writes them to TimescaleDB using the COPY protocol. |
| **COPY protocol** | PostgreSQL bulk write method (via asyncpg `copy_records_to_table`). 100x faster than row-by-row INSERT. |
| **Attribute service** | Consumes tracklet messages, applies quality gating and white balance correction, runs color classification via Triton, and persists color attributes to the database. |
| **Event engine** | Monitors track state changes and generates events (entered_scene, exited_scene, stopped, loitering, appeared, vanished) using per-track finite state machines. |
| **Clip service** | Generates video clips and thumbnails for closed events, uploads to MinIO, and updates event records with asset URIs. |

### Query

| Term | Definition |
|------|------------|
| **Query API** | FastAPI-based REST API for searching detections, tracks, and events. Supports JWT authentication, RBAC, camera scope filtering, and signed MinIO URLs. |
| **Signed URL** | Time-limited (1 hour) pre-signed URL for accessing MinIO objects (frame blobs, event clips, thumbnails) through the API without direct MinIO access. |
| **RBAC** | Role-Based Access Control. Four roles: `admin`, `operator`, `viewer`, `engineering`. |

---

## Infrastructure Terms

| Term | Definition |
|------|------------|
| **TimescaleDB** | PostgreSQL extension for time-series data. Used for high-volume detection and track observation storage with automatic chunk management. |
| **Hypertable** | A TimescaleDB virtual table that automatically partitions data into time-based chunks. Used for `detections` and `track_observations`. |
| **Chunk** | A partition of a hypertable covering a specific time interval (1 hour in this platform). Chunks are the unit of compression and retention. |
| **Compression policy** | TimescaleDB policy that compresses chunks older than 2 days. Achieves 12-15x compression ratio. |
| **Retention policy** | TimescaleDB policy that drops chunks older than 30 days. Applies to `detections` and `track_observations` hypertables. |
| **Lifecycle policy** | MinIO bucket policy that automatically expires objects after a configured number of days. Defined in `infra/minio/lifecycle-policies.json`. |
| **Storage tier** | Classification of MinIO buckets by access frequency: **hot** (7-day retention, frequently accessed), **warm** (30-90 day retention), **cold** (30-day retention, infrequently accessed). |
| **Kafka** | Distributed message queue used as the central data bus. 7 topics, 3-broker cluster with replication factor 3. |
| **Kafka consumer lag** | The number of messages a consumer group has not yet processed. The primary indicator of whether a service is keeping up with its input. |
| **Consumer group** | A Kafka concept where multiple consumer instances share the work of reading from a topic. Each partition is consumed by exactly one member of the group. |
| **NATS JetStream** | Lightweight message broker used at the edge. Provides persistent messaging with 10GB/24h buffer capacity. |
| **Schema Registry** | Confluent Schema Registry. Validates that all Kafka messages conform to their registered protobuf schema. Enforces backward compatibility. |
| **MinIO** | S3-compatible object storage. Stores frame blobs, event clips, thumbnails, debug traces, MTMC checkpoints, and archived media. |
| **Redis** | In-memory cache. Used for rate limiting, JWT blacklist, camera health state, and computation cache (256MB max). |
| **Protobuf** | Protocol Buffers. Binary serialization format used for all inter-service Kafka messages. More compact and faster than JSON. |

---

## Monitoring Terms

| Term | Definition |
|------|------------|
| **Consumer lag** | Number of unprocessed messages in a Kafka topic for a consumer group. The most important throughput health signal. |
| **Spool fill** | Percentage of the ingress bridge NVMe spool capacity in use. Rises when Kafka is slow or unreachable. |
| **Bridge throughput** | Messages per second flowing through the ingress bridge from NATS to Kafka. |
| **Write latency** | Time for the bulk collector to complete a COPY batch write to TimescaleDB. |
| **Staged rows** | Detection/tracklet/event rows buffered in the bulk collector awaiting the next flush. |
| **Queue delay** | Time an inference request waits in the Triton queue before GPU execution begins. |
| **Motion duty cycle** | Fraction of time with motion detected on a given camera. |
| **Checkpoint lag** | Seconds since the MTMC service last successfully wrote a FAISS checkpoint. |
| **Clock skew** | Difference in system time between two hosts. Monitored by Chrony-based clock drift collector. Critical for cross-camera event ordering. |

---

## Model Terms

| Term | Definition |
|------|------------|
| **Confidence threshold** | Minimum detection confidence score (0.40) for a detection to be kept after NMS. Below this threshold, detections are discarded. |
| **NMS** | Non-Maximum Suppression. Post-detection filtering that removes overlapping bounding boxes. |
| **ByteTrack** | Single-camera tracker that associates detections across frames using motion prediction (Kalman filter) and two-stage confidence matching (Hungarian algorithm). |
| **OSNet** | Re-ID embedding model. Produces 512-dimensional vectors for visual appearance comparison. |
| **FAISS** | Vector similarity search library used for real-time Re-ID matching. Flat index for exact search at <1ms latency. |
| **FP16** | Half-precision floating point. Used by TensorRT for inference, providing 2x throughput vs FP32 with negligible accuracy loss. |
| **TensorRT** | NVIDIA inference optimization toolkit. Converts ONNX models to optimized GPU engines with operator fusion and FP16 conversion. |
| **ONNX** | Open Neural Network Exchange. Model interchange format used between PyTorch training and Triton deployment. |
| **Shadow deployment** | Running a new model version alongside the production model to compare results before cutover (see [Model Rollout SOP](../runbooks/model-rollout-sop.md)). |
| **Bounding box** | Rectangle (x, y, width, height) indicating where an object was detected in a frame. |
| **Embedding** | A fixed-length numeric vector (512 dimensions for OSNet) representing the visual appearance of a detected object. Two embeddings with high cosine similarity likely depict the same object. |
| **Kalman filter** | Motion prediction model used in ByteTrack. Predicts where each tracked object should appear in the next frame based on its velocity and acceleration. |

---

## Event Types

The event engine produces 6 event types:

| Event | Description |
|-------|-------------|
| `entered_scene` | Object first appeared in a camera's field of view. |
| `exited_scene` | Object left a camera's field of view. |
| `stopped` | Object has been stationary for longer than the configured threshold. |
| `loitering` | Object has remained in a defined zone for longer than the configured threshold. |
| `appeared` | Object appeared suddenly (not from an edge of the field of view). |
| `vanished` | Object disappeared suddenly (not at an edge of the field of view). |
