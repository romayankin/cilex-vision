---
version: "1.0.0"
status: P2-X02
created_by: codex-cli
date: "2026-04-10"
---

# Service Restart Runbook

**Related documents:** `docs/deployment-guide-pilot.md`, `infra/ansible/playbooks/deploy-all.yml`, `infra/ansible/playbooks/deploy-multi-node.yml`, `scripts/deploy/health-check-all.sh`
**Scope:** Safe restart procedures for infrastructure and application services in dependency order.

---

## Overview

### Dependency Order

Restart services in this order:

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

### Environment Notes

- In the pilot, the restart commands use `pilot-*` container names.
- In multi-node deployments, the default container names are normally the service names from Ansible, for example `mtmc-service`, `event-engine`, `clip-service`.
- `attribute-service`, `event-engine`, `clip-service`, and `mtmc-service` may not be present in the CPU-only pilot. Skip those sections unless the service is actually deployed.
- After any restart, verify the service before restarting the next dependent service.

---

## TimescaleDB

**Dependency order:** first
**Restart command:**
```bash
docker restart pilot-timescaledb
```
**State recovery:** No data is lost if the PostgreSQL volume is intact. Writers and readers will reconnect after the service returns.
**Health verification:**
```bash
docker exec pilot-timescaledb pg_isready -U cilex -d vidanalytics
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "SELECT now();"
```

---

## MinIO

**Dependency order:** after TimescaleDB
**Restart command:**
```bash
docker restart pilot-minio pilot-minio-init
```
**State recovery:** Objects persist on disk. In-flight uploads fail and must be retried by the application.
**Health verification:**
```bash
curl -fsS http://localhost:9000/minio/health/live
docker exec pilot-minio-init mc ls local
```

---

## NATS

**Dependency order:** after MinIO
**Restart command:**
```bash
docker restart pilot-nats
```
**State recovery:** JetStream data remains on disk. Edge publishing pauses during restart; edge-agent local buffer should absorb short outages.
**Health verification:**
```bash
curl -fsS http://localhost:8222/healthz
curl -fsS http://localhost:8222/jsz | python3 -m json.tool | head -40
```

---

## Kafka

**Dependency order:** after NATS
**Restart command:**
```bash
docker restart pilot-kafka
```
**State recovery:** Kafka topic data persists on broker storage. Consumers will rebalance when the broker returns.
**Health verification:**
```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --list
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --all-groups --describe
```

---

## Triton

**Dependency order:** after Kafka
**Restart command:**
```bash
docker restart pilot-triton
```
**State recovery:** In pilot mode, the CPU model reloads from the mounted model repo. In EXPLICIT mode deployments, models may need to be loaded again after restart.
**Health verification:**
```bash
curl -fsS http://localhost:8000/v2/health/ready
curl -fsS http://localhost:8002/metrics | head -20
```
If EXPLICIT mode is enabled, reload the required models:
```bash
curl -s -X POST http://localhost:8000/v2/repository/models/yolov8l/load
curl -s -X POST http://localhost:8000/v2/repository/models/osnet/load
curl -s -X POST http://localhost:8000/v2/repository/models/color_classifier/load
curl -s -X POST http://localhost:8000/v2/repository/models/osnet_reid/load
```

---

## edge-agent

**Dependency order:** after Triton
**Restart command:**
```bash
docker restart pilot-edge-agent
```
**State recovery:** RTSP sessions reconnect. Local buffer content survives if it is stored on disk. No long-term metadata is lost.
**Health verification:**
```bash
docker logs pilot-edge-agent --tail 50
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=edge_camera_uptime_ratio'
```

---

## ingress-bridge

**Dependency order:** after edge-agent
**Restart command:**
```bash
docker restart pilot-ingress-bridge
```
**State recovery:** On-disk spool survives a normal container restart. In-flight NATS messages may be replayed.
**Health verification:**
```bash
docker logs pilot-ingress-bridge --tail 50
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=bridge_spool_fill_pct'
```

---

## decode-service

**Dependency order:** after ingress-bridge
**Restart command:**
```bash
docker restart pilot-decode-service
```
**State recovery:** In-memory sampler state resets. New frames are consumed after Kafka polling resumes.
**Health verification:**
```bash
docker logs pilot-decode-service --tail 50
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=decode_frames_consumed_total'
```

---

## inference-worker

**Dependency order:** after decode-service
**Restart command:**
```bash
docker restart pilot-inference-worker
```
**State recovery:** In-memory tracker state is lost. Expect a short track continuity gap while the tracker rebuilds active tracks.
**Health verification:**
```bash
docker logs pilot-inference-worker --tail 50
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=inference_detections_total'
```

---

## attribute-service

**Dependency order:** after inference-worker
**Restart command:**
```bash
docker restart attribute-service
```
**State recovery:** In-memory classification windows reset. Track attributes continue updating when new tracklets arrive.
**Health verification:**
```bash
docker ps --filter name=attribute-service
docker logs attribute-service --tail 50
```

---

## event-engine

**Dependency order:** after attribute-service
**Restart command:**
```bash
docker restart event-engine
```
**State recovery:** In-memory FSM state is lost. Open `stopped` or `loitering` duration events are not restored from PostgreSQL.
**Health verification:**
```bash
docker ps --filter name=event-engine
docker logs event-engine --tail 50
```

---

## clip-service

**Dependency order:** after event-engine
**Restart command:**
```bash
docker restart clip-service
```
**State recovery:** Temporary extraction files are lost. Closed events can be reprocessed because clip generation is deduped against the DB.
**Health verification:**
```bash
docker ps --filter name=clip-service
docker logs clip-service --tail 50
```

---

## mtmc-service

**Dependency order:** after clip-service
**Restart command:**
```bash
docker restart mtmc-service
```
**State recovery:** MTMC restores FAISS state from its local or MinIO checkpoint. Matching may be degraded until checkpoint restore finishes.
**Health verification:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_matches_total'
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_checkpoint_lag_seconds'
docker logs mtmc-service --tail 50
```

---

## bulk-collector

**Dependency order:** after mtmc-service
**Restart command:**
```bash
docker restart pilot-bulk-collector
```
**State recovery:** Staged rows in memory are lost if they were not flushed, but Kafka replay should refill them because offsets are committed only after successful writes.
**Health verification:**
```bash
docker logs pilot-bulk-collector --tail 50
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=bulk_rows_written_total'
```

---

## query-api

**Dependency order:** after bulk-collector
**Restart command:**
```bash
docker restart pilot-query-api
```
**State recovery:** No durable in-memory state is required. Existing client sessions retry after the service returns.
**Health verification:**
```bash
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8080/ready
```

---

## Monitoring

**Dependency order:** last
**Restart command:**
```bash
docker restart pilot-prometheus pilot-grafana
```
If running the Ansible multi-node stack, also restart:
```bash
docker restart prometheus grafana loki node-exporter promtail
```
**State recovery:** Grafana dashboards reload from provisioned files. Prometheus resumes scraping after restart. Short metric gaps are normal.
**Health verification:**
```bash
curl -fsS http://localhost:9090/-/healthy
curl -fsS http://localhost:3000/api/health
```

---

## Full Stack Restart

### Pilot

Run each command only after the previous service passes its verification step.

```bash
docker restart pilot-timescaledb
docker restart pilot-minio pilot-minio-init
docker restart pilot-nats
docker restart pilot-kafka
docker restart pilot-triton
docker restart pilot-edge-agent
docker restart pilot-ingress-bridge
docker restart pilot-decode-service
docker restart pilot-inference-worker
docker restart pilot-bulk-collector
docker restart pilot-query-api
docker restart pilot-prometheus pilot-grafana
```

### Multi-Node

Use the service order above and restart only the affected host group. If the change is larger than a simple restart, use Ansible:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-multi-node.yml
```

### Final Verification

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

If any check fails, stop the restart sequence and use `docs/runbooks/incident-response.md`.
