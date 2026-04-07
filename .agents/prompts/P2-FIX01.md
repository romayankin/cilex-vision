# P2-FIX01: Pilot Readiness Fixes

You are working across multiple services in /repo. This task resolves the Tier 1 blocking items from `todo_before_deployment.md` that prevent the pilot deployment from functioning correctly.

## 1. Kafka Topic Gaps

Add two missing topics to `infra/kafka/topics.yaml`:
- `frames.decoded.refs` — published by decode service (P1-V03), consumed by inference worker. Use same partition count and retention as `frames.sampled.refs`.
- `bulk.detections` — published by inference worker (P1-V04), consumed by bulk collector (P1-V05). Use same partition count and retention as `tracklets.local`.

## 2. Inference Worker Input Topic Reconfiguration

In `services/inference-worker/config.py`:
- Change the default `input_topic` from `frames.sampled.refs` to `frames.decoded.refs` so the inference worker consumes from the decode service output when deployed.
- Ensure the env var override still works (`INFERENCE_INPUT_TOPIC` or equivalent).

## 3. Wire TraceCollector into Inference Worker

In `services/inference-worker/main.py`:
- Replace or augment the existing `DebugTracer` usage with the `TraceCollector` from P1-V07.
- Call `TraceCollector.ensure_bucket()` during startup (after MinIO client is created).
- In the pipeline loop, call the enrichment methods: `should_collect()`, `begin()`, `collect_raw_detections()`, `collect_post_nms_detections()`, `collect_tracker_delta()`, `set_model_versions()`, `store()`.
- Keep the existing `DebugTracer` stage timing if it's still useful, or migrate its functionality into `TraceCollector`.

## 4. Register Topology Router in Query API

In `services/query-api/main.py`:
- Import and register the topology router from `services/topology/api.py`.
- The topology module lives outside query-api's directory, so add the appropriate sys.path or restructure the import. Prefer copying `services/topology/models.py` and `services/topology/api.py` into `services/query-api/routers/topology.py` with a local models import, keeping the topology service's test suite passing.

## 5. Update Existing Tests

- Ensure all existing tests still pass after the input_topic change and TraceCollector wiring.
- Add at least one test verifying the new `frames.decoded.refs` and `bulk.detections` topics exist in `topics.yaml`.
- Add at least one test verifying the topology router is registered and responds to `GET /topology/{site_id}`.

## Constraints

- Do NOT break any existing service's Dockerfile, config, or test suite.
- Do NOT change the bulk collector, decode service, or edge agent — only inference worker and query API are modified.
- Run `python3 -m ruff check` on all changed files.
- Run all affected test suites: inference-worker, query-api, topology.

## Expected Deliverables

- infra/kafka/topics.yaml (2 new topics)
- services/inference-worker/config.py (input_topic default change)
- services/inference-worker/main.py (TraceCollector wiring)
- services/query-api/main.py (topology router registration)
- Updated/new tests
- .agents/handoff/P2-FIX01.md
