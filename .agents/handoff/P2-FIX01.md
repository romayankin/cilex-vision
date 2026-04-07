# P2-FIX01: Pilot Readiness Fixes — Handoff

## What was done

Resolved five Tier 1 blocking items from `todo_before_deployment.md` that prevented the pilot deployment from functioning correctly.

### 1. Kafka Topic Gaps (`infra/kafka/topics.yaml`)

Added two missing topics:
- **`frames.decoded.refs`** — partitions=12, retention=2h, key=camera_id, value=FrameRef. Inserted after `frames.sampled.refs` with matching schema.
- **`bulk.detections`** — partitions=12, retention=6h, key=camera_id, value=Detection. Inserted after `tracklets.local` with matching retention.

### 2. Inference Worker Input Topic (`services/inference-worker/config.py`)

Changed `KafkaConfig.input_topic` default from `frames.sampled.refs` to `frames.decoded.refs`. Env var override (`INFERENCE__KAFKA__INPUT_TOPIC`) still works via pydantic-settings.

### 3. TraceCollector Wiring (`services/inference-worker/main.py`)

Replaced `DebugTracer` with `TraceCollector` (P1-V07) throughout:
- **Startup**: `TraceCollector(sample_rate, low_confidence_threshold, minio_client, bucket)` created conditionally when `debug.enabled=True`. Calls `ensure_bucket()` (creates bucket + 30-day lifecycle) at startup.
- **Pipeline enrichments**: `should_collect()`, `begin()` (with kafka_offset, source_capture_ts, edge_receive_ts, core_ingest_ts), `collect_post_nms_detections()`, `collect_tracker_delta()`, `set_model_versions()`, `store()`.
- **Tracker delta**: Captures `active_before` count before `tracker.update()`, then reports new/closed track IDs.
- **Model versions**: Records `triton.detector_model` and `triton.embedder_model` on each trace.
- **Storage key**: Date-partitioned `{camera_id}/{YYYY-MM-DD}/{trace_id}.json` (was flat `traces/{camera_id}/{trace_id}.json`).
- **Removed** redundant debug bucket creation from `_create_minio()` — now handled by `TraceCollector.ensure_bucket()`.
- **Source timestamp extraction**: Added `source_capture_ts` parsing from protobuf `VideoTimestamp`.

### 4. Topology Router in Query API (`services/query-api/main.py`)

Copied `services/topology/models.py` → `services/query-api/routers/topology_models.py` and `services/topology/api.py` → `services/query-api/routers/topology.py` (with import path adjusted). Registered `topology.router` in `create_app()`. The topology service's original test suite still passes unchanged.

### 5. Tests

- **`infra/kafka/test_topics.py`** — 4 tests validating `frames.decoded.refs` and `bulk.detections` exist with correct partition count, key schema, and value schema.
- **`services/query-api/tests/test_topology.py`** — 4 tests: GET returns topology graph, empty site returns empty lists, unauthenticated returns 401, viewer role returns 403.
- All pre-existing test suites pass: query-api (52), inference-worker, topology (37).
- Ruff check clean on all changed files.

## Key decisions

| Decision | Rationale |
|----------|-----------|
| Copy topology files into query-api rather than sys.path hack | Per task spec. Keeps both services independently testable. |
| Conditional TraceCollector creation based on `debug.enabled` | TraceCollector has no `enabled` flag; setting `sample_rate=0` wouldn't prevent low-confidence triggers. Null check is simpler. |
| Convert `sample_rate_pct` (2.0) → fraction (0.02) | TraceCollector uses fraction, DebugTracer used percentage. `sample_rate_pct / 100.0`. |
| Remove bucket creation from `_create_minio()` | `TraceCollector.ensure_bucket()` handles both creation and lifecycle policy; avoids duplication. |
| Keep `trace.labels["embeddings_extracted"]` alongside new enrichments | Lightweight metric, no conflict with TraceCollector fields. |

## Files changed

- `infra/kafka/topics.yaml` — 2 new topic definitions
- `services/inference-worker/config.py` — `input_topic` default
- `services/inference-worker/main.py` — TraceCollector wiring
- `services/query-api/main.py` — topology router import + registration
- `services/query-api/routers/topology.py` — copied + adapted from topology service
- `services/query-api/routers/topology_models.py` — copied from topology service

## Files added

- `infra/kafka/test_topics.py`
- `services/query-api/tests/test_topology.py`

## Gotchas for reviewers

- The `topology.py` router in query-api has its own inline JWT auth via `_get_current_user`. This is redundant with query-api's `AuditMiddleware` for logging, but the Depends-based auth is still needed for per-endpoint RBAC (admin vs operator roles).
- `collect_raw_detections()` is not called because the `DetectorClient.detect()` method returns post-NMS detections only. Pre-NMS capture would require changes to the detector client (out of scope).
- `collect_attributes()` is not called because the inference worker doesn't run attribute extraction — that's a separate service consuming from `attributes.jobs`.
