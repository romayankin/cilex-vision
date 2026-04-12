# Disaster Recovery

**Scope:** Backup, restore, and failover procedures for the Cilex Vision platform across pilot and multi-node deployments.

## Recovery Targets

| Data class | RPO | RTO | Recovery source |
|---|---:|---:|---|
| PostgreSQL / TimescaleDB | 15 min | 2 hours | `infra/backup/backup-db.sh` + `infra/restore/restore-db.sh` |
| MinIO event clips / thumbnails | 1 hour | 4 hours | `infra/backup/backup-minio.sh` + `infra/restore/restore-full.sh` |
| Raw / decoded frames | 4 hours | 8 hours | `infra/backup/backup-minio.sh` + `infra/restore/restore-full.sh` |
| MTMC checkpoints | 5 min | 30 min | `infra/backup/backup-config.sh` snapshot + `restore-full.sh` MinIO restore |
| Kafka offsets snapshot | 15 min | 30 min | documented operator snapshot procedure from `docs/runbooks/backup-restore.md` |
| Ansible config / PKI / Terraform / `.env` | 24 hours | 1 hour | `infra/backup/backup-config.sh` |

## Automation Assets

| Asset | Purpose |
|---|---|
| `infra/backup/backup-db.sh` | Logical PostgreSQL / TimescaleDB backups (`pg_dump -Fc` + globals) |
| `infra/backup/backup-minio.sh` | Bucket mirroring with `mc mirror --overwrite --remove` |
| `infra/backup/backup-config.sh` | Configuration and PKI archive plus `mtmc-checkpoints` snapshot |
| `infra/restore/restore-db.sh` | Full DB restore with table verification |
| `infra/restore/restore-full.sh` | End-to-end restore orchestration and health verification |
| `infra/failover/health-watchdog.py` | Continuous service / lag / disk watchdog with optional webhook alerts |
| `scripts/test-restore.sh` | Isolated TimescaleDB restore drill in a temporary container |

## Recommended Backup Schedule

### Cron examples

Database every 15 minutes:

```cron
*/15 * * * * cd /opt/cilex/cilex-vision && infra/backup/backup-db.sh >> /var/log/cilex/backup-db.log 2>&1
```

MinIO every hour for hot/warm/cold buckets:

```cron
0 * * * * cd /opt/cilex/cilex-vision && BACKUP_ENDPOINT=https://backup.example.internal infra/backup/backup-minio.sh >> /var/log/cilex/backup-minio.log 2>&1
```

Config and PKI nightly:

```cron
30 2 * * * cd /opt/cilex/cilex-vision && infra/backup/backup-config.sh >> /var/log/cilex/backup-config.log 2>&1
```

Watchdog every 30 seconds under systemd or process supervisor:

```bash
python3 infra/failover/health-watchdog.py --prometheus http://localhost:9090 --interval 30
```

## Restore Procedures

### 1. Configuration and PKI

Restore the config archive first so Ansible inventory, PKI, and Terraform files are back in place before service recovery:

```bash
infra/restore/restore-full.sh \
  --db-backup /backups/postgres/latest.dump \
  --config-backup /backups/config/latest.tar.gz
```

The full restore script extracts the config archive into the requested restore root, restores the database, restores MinIO objects when a backup endpoint is configured, then calls `scripts/deploy/health-check-all.sh`.

### 2. Database-only recovery

Use when object storage is intact and only TimescaleDB / PostgreSQL must be restored:

```bash
infra/restore/restore-db.sh \
  --backup-file /backups/postgres/vidanalytics-20260412-010000.dump \
  --globals-file /backups/postgres/vidanalytics-globals-20260412-010000.sql
```

### 3. Test restore

Run a monthly isolated restore drill:

```bash
scripts/test-restore.sh --db-backup /backups/postgres/latest.dump
```

The script starts a temporary TimescaleDB container, restores the dump, verifies critical tables (`sites`, `cameras`, `detections`, `local_tracks`, `events`), and then removes the container.

## Failover Procedures

### TimescaleDB unavailable

1. Confirm outage with `infra/failover/health-watchdog.py` or `scripts/deploy/health-check-all.sh`.
2. Stop write-heavy services if needed:
   `docker stop pilot-bulk-collector pilot-query-api pilot-inference-worker`
3. Restore the latest logical backup with `infra/restore/restore-db.sh`.
4. Restart services and confirm table counts plus API health.

### MinIO unavailable

1. Restore MinIO service availability.
2. Re-run `infra/restore/restore-full.sh` with the remote backup endpoint configured.
3. Verify bucket health and signed-URL consumers (`query-api`, `clip-service`, `mtmc-service`).

### Kafka broker or lag incident

1. Use the watchdog and Grafana / Prometheus to confirm consumer lag or broker loss.
2. Recover the broker per the Kafka runbook.
3. Compare consumer positions with the latest offset snapshot from the backup runbook.
4. If needed, reset or replay consumers using the saved offset reference.

## Monthly DR Drill Checklist

1. Confirm latest DB, MinIO, and config backups exist and are readable.
2. Run `scripts/test-restore.sh` against the latest DB dump.
3. Run a staged `restore-full.sh` into a non-production environment.
4. Verify `scripts/deploy/health-check-all.sh` passes after restore.
5. Record actual RPO / RTO versus the targets above.
6. Capture gaps and append them to `todo_before_deployment.md` or the next ops handoff.

## Escalation Contacts

Replace these placeholders before production rollout:

| Function | Contact |
|---|---|
| Platform on-call | `REPLACE_ME` |
| Database owner | `REPLACE_ME` |
| Storage / MinIO owner | `REPLACE_ME` |
| Security / PKI owner | `REPLACE_ME` |
| Site operations | `REPLACE_ME` |

