# Maintenance Windows

Procedures for scheduled maintenance of the Cilex Vision platform. All maintenance should be performed during low-activity hours and announced to operations staff in advance.

---

## Pre-Maintenance Checklist

Complete before starting any maintenance window:

- [ ] Notify operations staff of the maintenance window, expected duration, and affected services.
- [ ] Verify current backups are complete and recent (see [Backup and Restore](../runbooks/backup-restore.md)).
- [ ] Capture baseline health:
  ```bash
  scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
  ```
- [ ] Check Prometheus for any currently firing alerts. Resolve critical alerts before proceeding.
- [ ] Save current infrastructure state:
  ```bash
  mkdir -p artifacts/maintenance-backups
  tar -czf "artifacts/maintenance-backups/pre-maint-$(date +%Y%m%d-%H%M%S).tgz" \
    infra/ansible infra/pilot scripts/deploy
  ```
- [ ] Confirm a rollback plan exists for each change being made.

---

## Service Restarts

Follow the dependency order documented in [Service Restart Runbook](../runbooks/service-restart.md):

1. TimescaleDB
2. MinIO
3. NATS
4. Kafka
5. Triton
6. edge-agent
7. ingress-bridge
8. decode-service
9. inference-worker
10. attribute-service
11. event-engine
12. clip-service
13. mtmc-service
14. bulk-collector
15. query-api
16. monitoring

**Key rule:** Restart one service at a time. Verify health before proceeding to the next.

---

## TimescaleDB Maintenance

### Verify Compression Policy

Compression is configured for 2-day-old chunks. Verify it is running:

```bash
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "
  SELECT hypertable_name, older_than
  FROM timescaledb_information.compression_settings;
"
```

### Verify Retention Policy

30-day retention is configured for hypertables. Verify:

```bash
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "
  SELECT hypertable_name, schedule_interval, config
  FROM timescaledb_information.jobs
  WHERE proc_name = 'policy_retention';
"
```

### Check Chunk Health

```bash
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "
  SELECT hypertable_name,
         chunk_name,
         range_start,
         range_end,
         is_compressed
  FROM timescaledb_information.chunks
  ORDER BY range_end DESC
  LIMIT 20;
"
```

### Refresh Transit-Time Materialized View

The `transit_time_stats` materialized view is refreshed by the adaptive transit-time pipeline. To manually refresh:

```bash
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "
  REFRESH MATERIALIZED VIEW CONCURRENTLY transit_time_stats;
"
```

### VACUUM and ANALYZE

For relational tables (non-hypertable), run periodically:

```bash
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "
  VACUUM ANALYZE cameras;
  VACUUM ANALYZE topology_edges;
  VACUUM ANALYZE global_tracks;
  VACUUM ANALYZE global_track_links;
  VACUUM ANALYZE events;
  VACUUM ANALYZE calibration_results;
"
```

### Verification

Check Storage dashboard (`/d/storage`) for write latency and write errors after maintenance.

---

## MinIO Maintenance

### Verify Lifecycle Policies

Lifecycle policies are defined in `infra/minio/lifecycle-policies.json`:

| Bucket | Tier | Expiration |
|--------|------|-----------|
| frame-blobs | hot | 7 days |
| decoded-frames | hot | 7 days |
| event-clips | warm | 90 days |
| thumbnails | warm | 30 days |
| raw-video | cold | 30 days |
| archive-warm | warm | 90 days |
| debug-traces | cold | 30 days |
| mtmc-checkpoints | hot | 7 days |

Apply or re-apply lifecycle policies:

```bash
python3 scripts/minio/apply_lifecycle.py --config infra/minio/lifecycle-policies.json
```

### Storage Report

Check per-bucket usage:

```bash
docker exec pilot-minio-init mc du --depth 1 local
```

### Verify Bucket Health

```bash
docker exec pilot-minio-init mc ls local
curl -fsS http://localhost:9000/minio/health/live
```

### Verification

Check Storage Tiering dashboard (`/d/storage-tiering`) for bucket sizes and lifecycle activity.

---

## Kafka Maintenance

### Topic Inspection

List all topics and verify they match the canonical definitions:

```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --list
```

### Consumer Group Status

Check all consumer groups for lag:

```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --all-groups --describe
```

### Consumer Group Reset

If a consumer group needs to be reset (e.g., after a reprocessing event), use with caution:

```bash
# Stop the affected consumer service first
docker stop <service-container>

# Reset to latest (skip all pending messages)
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group <group-name> \
  --topic <topic-name> \
  --reset-offsets --to-latest --execute

# Restart the service
docker start <service-container>
```

**Warning:** Resetting consumer offsets will skip unprocessed messages. Only do this when you explicitly want to discard pending data.

### Verification

Check Bus Health dashboard (`/d/bus-health`) for consumer lag and throughput.

---

## Calibration Baseline Refresh

The calibration scheduler runs automatically (02:00 UTC by default). To trigger a manual calibration run:

```bash
python3 scripts/calibration/calibration_scheduler.py \
  --db-dsn "postgresql://cilex:cilex@localhost:5432/vidanalytics"
```

### Verify Results

```bash
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "
  SELECT camera_id, calibrated_at, status
  FROM calibration_results
  ORDER BY calibrated_at DESC
  LIMIT 10;
"
```

Check Prometheus textfile metrics for `calibration_*` values.

---

## Drift Baseline Recapture

The drift detector compares current confidence distributions against a stored baseline. Recapture the baseline when:

- A new model version has been deployed and validated.
- The baseline is older than 30 days.
- Environmental conditions have permanently changed (new camera angles, lighting changes).

### Capture New Baseline

```bash
python3 scripts/monitoring/baseline_snapshot.py \
  --db-dsn "postgresql://cilex:cilex@localhost:5432/vidanalytics" \
  --output "s3://debug-traces/baselines/confidence-baseline.json" \
  --window-hours 168
```

The `--window-hours 168` flag uses the last 7 days of data for a representative distribution.

### Verify New Baseline

```bash
python3 scripts/monitoring/drift_detector.py \
  --db-dsn "postgresql://cilex:cilex@localhost:5432/vidanalytics" \
  --baseline "s3://debug-traces/baselines/confidence-baseline.json" \
  --report artifacts/monitoring/drift-report.md \
  --prom-file /dev/null
```

If no drift is flagged against the new baseline, the recapture was successful.

---

## Post-Maintenance Verification

After completing all maintenance tasks:

- [ ] Run the full health check:
  ```bash
  scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
  ```
- [ ] Verify all Grafana dashboards show normal operation:
  - Stream Health (`/d/stream-health`): all cameras online
  - Inference Performance (`/d/inference-performance`): detections flowing
  - Bus Health (`/d/bus-health`): no spool buildup
  - Storage (`/d/storage`): writes succeeding
- [ ] Verify no new alerts are firing in Prometheus.
- [ ] Notify operations staff that the maintenance window is complete.
- [ ] Document any issues encountered during maintenance.
