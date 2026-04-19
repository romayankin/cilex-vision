# Deferred work after 10-phase motion-events refactor

Shipped in Phases 1-10 (April 19, 2026): motion-events-as-primary-artifact
refactor — events.metadata_jsonb schema, metadata aggregator, polymorphic
clip URIs, server-side segment concat, AI search adapted, UI rewrites.

## Intentionally deferred

### Legacy event types still in DB + state_machine.py

`entered_scene`, `exited_scene`, `stopped`, `loitering` are still emitted
by `services/event-engine/state_machine.py` on tracklet transitions. They
are NOT surfaced in `/search` or `/timeline` (both filter to
`event_type='motion'`). They accumulate quietly in the DB.

To fully remove:
- Gut state_machine.py EventType enum down to motion-only
- Remove references from event-engine main.py (`_DEDUP_EVENT_TYPES`, cooldown)
- Remove references from nlp_search.py, zones.py, Timeline.tsx, Pictograms.tsx
- Tighten `CHECK (event_type = 'motion')` on events table
- Bulk DELETE of existing legacy rows

Estimated effort: 3-4 hours. Regression risk: moderate (state machine is
core to event-engine).

### Frame storage (frame-blobs + decoded-frames) as pipeline IPC

MinIO buckets `frame-blobs` and `decoded-frames` are used as message bus
between pipeline stages:
- edge-agent writes JPEGs → `frame-blobs`
- decode-service reads `frame-blobs`, writes decoded pixels to `decoded-frames`
- inference-worker reads `decoded-frames` for detection
- clip-service and lpr-service also read `decoded-frames`

To eliminate these buckets (and reduce disk churn), the pipeline needs
refactoring to use Kafka messages carrying binary payloads, or shared
memory, or a different IPC mechanism.

Estimated effort: 8-16 hours. Regression risk: high.

### clip_uri NOT NULL

Motion events can close without a clip when:
- Recorder is down / never started for this camera
- Segment race (Phase 9's fix handles most cases optimistically)

Rather than NOT NULL, the UI gracefully shows "No clip" for null-URI events.
If the operational fleet is reliable, this constraint could be tightened
after observing that null-URI events no longer occur in a stable week.

### Codec mislabel in recorder

recorder-service writes `codec='h264'` to video_segments, but segments are
actually encoded HEVC (h.265). Discovered during Phase 9 validation. Does
not affect playback (ffmpeg handles either). Fix: detect codec from stream
or from RTSP SDP and record accurately.
