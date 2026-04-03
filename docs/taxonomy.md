---
version: "0.0.0"
status: STUB — to be completed by task P0-D01
created_by: scaffold
---

# Taxonomy & Requirements Specification

> **⚠️ This is a placeholder.** The full specification will be produced by a DESIGN agent executing task **P0-D01**.
> Until then, downstream tasks should treat this file as incomplete.

## Object Classes (draft — not yet approved)

| Class | Confidence Threshold | Definition |
|-------|---------------------|------------|
| person | 0.4 | A human being, standing, walking, or seated |
| car | 0.4 | A passenger automobile |
| truck | 0.4 | A commercial freight vehicle |
| bus | 0.4 | A public or private passenger bus |
| bicycle | 0.4 | A human-powered two-wheeled vehicle |
| motorcycle | 0.4 | A motorized two-wheeled vehicle |
| animal | 0.4 | Any non-human animal |

## Attributes (draft)

| Attribute | Applies To | Values |
|-----------|-----------|--------|
| vehicle_color | car, truck, bus, motorcycle | red, blue, white, black, silver, green, yellow, brown, orange, unknown |
| person_upper_color | person | same as vehicle_color |
| person_lower_color | person | same as vehicle_color |

## Events (draft)

- entered_scene, exited_scene, stopped, loitering, motion_started, motion_ended

## Non-Functional Requirements (draft)

| NFR | Target | Measurement |
|-----|--------|-------------|
| End-to-end latency | <2s | Timestamp delta: edge_receive_ts to query-available |
| Inference FPS | 5–10 | Prometheus: inference_fps gauge |
| Pilot cameras | 4 | Configuration |
| Retention (raw) | 30 days | MinIO lifecycle policy |
| Retention (events) | 90 days | MinIO lifecycle policy |
| Retention (metadata) | 1 year | TimescaleDB retention policy |
| Query p95 latency | <500ms | Prometheus: query_latency_ms histogram |
