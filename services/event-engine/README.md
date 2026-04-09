# Event Engine

Consumes `tracklets.local`, maintains per-track state machines, emits `vidanalytics.v1.event.Event` protobufs to `events.raw`, and writes event rows to PostgreSQL.

## What it detects

- `entered_scene`
- `exited_scene`
- `stopped`
- `loitering`
- `motion_started`
- `motion_ended`

Track-derived events (`entered_scene`, `exited_scene`, `stopped`, `loitering`) come from the per-track FSM in `state_machine.py`.

Camera-level motion events are currently derived from tracklet activity timing because the core pipeline does not yet publish the edge motion-detector signal as its own Kafka stream. This keeps the service functional on the current repo state, but exact frame-motion semantics still need an upstream motion feed.

## Camera config JSON

`cameras.config_json` is parsed lazily per camera. The parser currently accepts a few polygon shapes because the repo has not standardized one canonical JSON structure yet:

```json
{
  "roi": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
  "loitering_zones": [
    {
      "zone_id": "zone-a",
      "duration_s": 30.0,
      "polygon": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    }
  ]
}
```

It also accepts point objects like `{"x": 0.2, "y": 0.8}`.

## Local validation

```bash
pytest services/event-engine/tests -q
ruff check services/event-engine
mypy --config-file services/event-engine/mypy.ini services/event-engine
docker build -f services/event-engine/Dockerfile . -t cilex-event-engine:test
```
