---
version: "1.0.0"
status: P2-X02
created_by: codex-cli
date: "2026-04-10"
---

# Backup and Restore Runbook

**Related documents:** `docs/runbooks/service-restart.md`, `docs/runbooks/model-rollout-sop.md`, `infra/pki/bootstrap-site.sh`, `infra/pki/step-ca-config.json`
**Scope:** Backup and restore procedures for Cilex Vision metadata, object storage, MTMC checkpoints, Kafka offsets, deployment config, and PKI materials.

---

## Overview

### What Must Be Backed Up

| Data class | Source | Why it matters |
|------------|--------|----------------|
| PostgreSQL / TimescaleDB | `vidanalytics` database | Canonical metadata: detections, tracks, events, cameras, topology |
| MinIO buckets | `frame-blobs`, `decoded-frames`, `event-clips`, `thumbnails`, `debug-traces`, `raw-video`, `archive-warm`, `mtmc-checkpoints` | Frame references, event clips, debug traces, retained media, MTMC remote checkpoints |
| MTMC local checkpoint | `/var/lib/cilex/mtmc-service/checkpoint` | Fast restore of FAISS live state |
| Kafka offsets snapshot | Kafka consumer groups | Restore point reference during incident recovery |
| Ansible and deployment config | `infra/ansible/`, `infra/pilot/`, `.env` files | Needed to rebuild the environment |
| PKI materials | `infra/pki/`, generated `/secure/sites/...` bundles | Required for NATS mTLS and site bootstrap |

### Recovery Targets

| Data class | Recommended RPO | Recommended RTO |
|------------|-----------------|-----------------|
| PostgreSQL / TimescaleDB | 15 minutes | 2 hours |
| MinIO event clips and thumbnails | 1 hour | 4 hours |
| Raw / decoded frames | 4 hours | 8 hours |
| MTMC checkpoints | 5 minutes | 30 minutes |
| Kafka offsets | 15 minutes | 30 minutes |
| Ansible config and PKI | 24 hours | 1 hour |

---

## 1. PostgreSQL / TimescaleDB Backup

### Full Logical Backup

Run from the pilot host:

```bash
mkdir -p backups/postgres
docker exec pilot-timescaledb pg_dump -U cilex -d vidanalytics -Fc \
  > "backups/postgres/vidanalytics-$(date +%Y%m%d-%H%M%S).dump"
docker exec pilot-timescaledb pg_dumpall -U cilex --globals-only \
  > "backups/postgres/vidanalytics-globals-$(date +%Y%m%d-%H%M%S).sql"
```

Run from a multi-node host:

```bash
ssh timescaledb-1.core.cilex.internal \
  'docker exec timescaledb pg_dump -U cilex -d vidanalytics -Fc' \
  > "backups/postgres/vidanalytics-$(date +%Y%m%d-%H%M%S).dump"
```

### Quick Verification

```bash
pg_restore -l backups/postgres/vidanalytics-YYYYMMDD-HHMMSS.dump | head -40
```

---

## 2. MinIO Bucket Backup

### Mirror All Buckets

Use a temporary `mc` container so the operator does not need to install MinIO tools on the host:

```bash
mkdir -p backups/minio
docker run --rm --network host -v "$PWD/backups/minio:/backups" minio/mc /bin/sh -c '
  mc alias set local http://localhost:9000 minioadmin minioadmin123 &&
  for bucket in frame-blobs decoded-frames event-clips thumbnails debug-traces raw-video archive-warm mtmc-checkpoints; do
    mc mirror --overwrite "local/${bucket}" "/backups/${bucket}";
  done
'
```

If a bucket does not exist in the current environment, `mc mirror` will fail for that bucket only. Remove it from the list for pilot environments that do not use it yet.

### Verification

```bash
find backups/minio -maxdepth 2 -type d | sort
find backups/minio/event-clips -type f | head -20
```

---

## 3. MTMC Checkpoint Backup

### Local Checkpoint Copy

```bash
mkdir -p backups/mtmc
tar -czf "backups/mtmc/mtmc-local-checkpoint-$(date +%Y%m%d-%H%M%S).tgz" \
  /var/lib/cilex/mtmc-service/checkpoint
```

If the deployment is remote:

```bash
ssh app-1.core.cilex.internal \
  'tar -czf - /var/lib/cilex/mtmc-service/checkpoint' \
  > "backups/mtmc/mtmc-local-checkpoint-$(date +%Y%m%d-%H%M%S).tgz"
```

### Remote Checkpoint Copy

The MTMC service can also store checkpoints in the `mtmc-checkpoints` MinIO bucket. Include that bucket in the MinIO mirror step above.

### Verification

```bash
tar -tzf backups/mtmc/mtmc-local-checkpoint-YYYYMMDD-HHMMSS.tgz | head -20
```

---

## 4. Kafka Offset Snapshot

### Capture Consumer Group State

Pilot:

```bash
mkdir -p backups/kafka
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --all-groups --describe \
  > "backups/kafka/consumer-groups-$(date +%Y%m%d-%H%M%S).txt"
```

Production:

```bash
ssh kafka-1.core.cilex.internal '
  docker exec kafka-1 /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9093 \
    --all-groups --describe \
    --command-config /opt/bitnami/kafka/config/admin.properties
' > "backups/kafka/consumer-groups-$(date +%Y%m%d-%H%M%S).txt"
```

### Important Note

This file is an operational snapshot. It is not a full Kafka backup. Use it to confirm recovery point or guide offset reset after a restore.

---

## 5. Config and PKI Backup

### Deployment Config

```bash
mkdir -p backups/config
tar -czf "backups/config/config-$(date +%Y%m%d-%H%M%S).tgz" \
  infra/ansible infra/pilot docs/runbooks scripts/deploy .agents
```

### PKI Materials

```bash
mkdir -p backups/pki
tar -czf "backups/pki/pki-$(date +%Y%m%d-%H%M%S).tgz" \
  infra/pki /secure/sites
```

If `/secure/sites` lives outside the repo, run the command from the host that stores the generated bundles.

### Verification

```bash
tar -tzf backups/pki/pki-YYYYMMDD-HHMMSS.tgz | head -40
python3 -m json.tool infra/pki/step-ca-config.json >/dev/null
bash -n infra/pki/bootstrap-site.sh
```

---

## 6. Recommended Backup Schedule

### Cron Examples

PostgreSQL every 15 minutes:

```cron
*/15 * * * * cd /opt/cilex && docker exec pilot-timescaledb pg_dump -U cilex -d vidanalytics -Fc > /var/backups/cilex/postgres/vidanalytics-$(date +\%Y\%m\%d-\%H\%M).dump
```

MinIO mirror every 4 hours:

```cron
0 */4 * * * cd /opt/cilex && docker run --rm --network host -v /var/backups/cilex/minio:/backups minio/mc /bin/sh -c 'mc alias set local http://localhost:9000 minioadmin minioadmin123 && for bucket in frame-blobs decoded-frames event-clips thumbnails debug-traces raw-video archive-warm mtmc-checkpoints; do mc mirror --overwrite "local/${bucket}" "/backups/${bucket}"; done'
```

Kafka offsets every 15 minutes:

```cron
*/15 * * * * cd /opt/cilex && docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --all-groups --describe > /var/backups/cilex/kafka/consumer-groups-$(date +\%Y\%m\%d-\%H\%M).txt
```

Config and PKI nightly:

```cron
30 2 * * * cd /opt/cilex && tar -czf /var/backups/cilex/config/config-$(date +\%Y\%m\%d).tgz infra/ansible infra/pilot infra/pki /secure/sites
```

---

## 7. Restore Procedure

### Restore Order

Restore in this order:

1. Deployment config and PKI
2. MinIO
3. PostgreSQL / TimescaleDB
4. Kafka offsets or broker volumes
5. MTMC checkpoints
6. Application services

### 7.1 Restore Config and PKI

```bash
tar -xzf backups/config/config-YYYYMMDD-HHMMSS.tgz -C /
tar -xzf backups/pki/pki-YYYYMMDD-HHMMSS.tgz -C /
```

Re-check:

```bash
python3 -m json.tool infra/pki/step-ca-config.json >/dev/null
bash -n infra/pki/bootstrap-site.sh
```

### 7.2 Restore MinIO

1. Ensure MinIO is running:

```bash
docker restart pilot-minio
curl -fsS http://localhost:9000/minio/health/live
```

2. Push data back to MinIO:

```bash
docker run --rm --network host -v "$PWD/backups/minio:/backups" minio/mc /bin/sh -c '
  mc alias set local http://localhost:9000 minioadmin minioadmin123 &&
  for bucket in frame-blobs decoded-frames event-clips thumbnails debug-traces raw-video archive-warm mtmc-checkpoints; do
    mc mb -p "local/${bucket}" || true
    mc mirror --overwrite "/backups/${bucket}" "local/${bucket}";
  done
'
```

3. Verify one known file from a critical bucket:

```bash
docker run --rm --network host minio/mc /bin/sh -c '
  mc alias set local http://localhost:9000 minioadmin minioadmin123 &&
  mc ls local/event-clips | head
'
```

### 7.3 Restore PostgreSQL / TimescaleDB

1. Stop application writers:

```bash
docker stop pilot-bulk-collector pilot-query-api pilot-ingress-bridge pilot-decode-service pilot-inference-worker
```

2. Restore globals:

```bash
cat backups/postgres/vidanalytics-globals-YYYYMMDD-HHMMSS.sql | \
  docker exec -i pilot-timescaledb psql -U cilex postgres
```

3. Restore the database:

```bash
docker exec -i pilot-timescaledb dropdb -U cilex --if-exists vidanalytics
docker exec -i pilot-timescaledb createdb -U cilex vidanalytics
cat backups/postgres/vidanalytics-YYYYMMDD-HHMMSS.dump | \
  docker exec -i pilot-timescaledb pg_restore -U cilex -d vidanalytics --clean --if-exists
```

4. Verify row presence:

```bash
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c "SELECT COUNT(*) FROM cameras;"
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c "SELECT COUNT(*) FROM events;"
```

### 7.4 Restore Kafka

Preferred method:

- restore the Kafka broker volume or VM disk snapshot taken at the same time as the database snapshot
- then compare live offsets to the saved offset snapshot

Pilot check:

```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --all-groups --describe
```

If consumers must be reset manually, do that only under platform engineering approval.

### 7.5 Restore MTMC Checkpoint

1. Stop MTMC:

```bash
docker stop mtmc-service
```

2. Restore the checkpoint files:

```bash
mkdir -p /var/lib/cilex/mtmc-service
tar -xzf backups/mtmc/mtmc-local-checkpoint-YYYYMMDD-HHMMSS.tgz -C /
```

3. Restart MTMC:

```bash
docker start mtmc-service
curl -fsS http://localhost:8080/metrics | grep mtmc_checkpoint_lag_seconds
```

### 7.6 Start the Stack

After state restore, start services in dependency order by following `docs/runbooks/service-restart.md`.

Quick pilot example:

```bash
docker restart pilot-timescaledb pilot-minio pilot-nats pilot-kafka pilot-triton
docker restart pilot-edge-agent pilot-ingress-bridge pilot-decode-service pilot-inference-worker
docker restart pilot-bulk-collector pilot-query-api
```

---

## 8. Post-Restore Verification

Run:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

Then verify:

```bash
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:9090/-/healthy
curl -fsS http://localhost:3000/api/health
```

Also check:

- Grafana Stream Health (`/d/stream-health`)
- Grafana Storage (`/d/storage`)
- Grafana MTMC Re-ID Health (`/d/mtmc-health`) if MTMC is enabled

---

## 9. Backup Test Requirement

At least once per month:

1. Restore the latest PostgreSQL backup into a staging database.
2. Restore one MinIO bucket into staging.
3. Restore one MTMC checkpoint.
4. Run:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

5. Record:
   - backup date
   - restore date
   - restore duration
   - pass or fail
   - issues found

Do not treat a backup as valid until a restore test has succeeded.
