# Metadata Bulk Collector

Consumes Kafka metadata messages, batches them in memory, and writes
high-volume rows to TimescaleDB with asyncpg `COPY`.

Current write targets:

- `detections`
- `track_observations`

Local validation:

```bash
python3 -m py_compile services/bulk-collector/*.py services/bulk-collector/tests/*.py
pytest services/bulk-collector/tests
python3 scripts/load-test/load-test-collector.py --cameras 5 --rate-per-camera 10 --duration-s 2
```

Runtime note:

- the collector expects `Detection` payloads plus Kafka headers
  `x-frame-seq`, `x-local-track-id` (optional), and `x-embedding-ref` (optional)
- the canonical detections topic is still a repo-level contract gap, so the
  service keeps the topic binding configurable in `config.py`
