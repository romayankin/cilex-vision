---
version: "1.0.0"
status: P2-X02
created_by: codex-cli
date: "2026-04-10"
---

# Scaling Runbook

**Related documents:** `docs/deployment-guide-pilot.md`, `infra/ansible/playbooks/deploy-multi-node.yml`, `infra/ansible/inventory/production.yml`, `infra/terraform/environments/production/main.tf`
**Scope:** Operator procedures for scaling the Cilex Vision platform from the 4-camera pilot toward multi-node production.

---

## Overview

| Scaling action | Lowest-risk path | Main verification |
|----------------|------------------|-------------------|
| Add cameras | Update edge config, topology, and calibration | Stream Health + query checks |
| Add GPU nodes | Use Terraform + Ansible `gpu-node` / `deploy-triton.yml` | Triton ready + `nvidia-smi` |
| Scale Kafka | Add broker host, deploy Kafka, then rebalance partitions | Consumer lag stable |
| Scale TimescaleDB | Add read replica, then adjust chunk policy only if needed | Write latency + query latency |
| Scale MinIO | Expand storage volume first; distributed MinIO later | MinIO health + free space |
| Add service instances | Add `services` hosts and re-run Ansible | `/metrics` or `/health` on new hosts |

### Before Any Scaling Change

1. Confirm backups are current by following `docs/runbooks/backup-restore.md`.
2. Notify operations staff of a change window.
3. Save the current inventory and config:

```bash
mkdir -p artifacts/scaling-backups
tar -czf "artifacts/scaling-backups/pre-scale-$(date +%Y%m%d-%H%M%S).tgz" \
  infra/ansible infra/pilot scripts/deploy
```

4. Capture baseline health:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

---

## 1. Adding Cameras

### Prerequisites

- Camera is physically installed, powered, and on the correct VLAN.
- RTSP credentials are known.
- A site ID already exists in the `sites` table.
- Query API and edge-agent are running.

### Pilot Procedure

1. Verify RTSP:

```bash
ffprobe -v quiet -rtsp_transport tcp -i "rtsp://USER:PASS@CAMERA_IP/STREAM"
```

2. Add the camera to the pilot config and DB:

```bash
bash scripts/pilot/add-camera.sh \
  --id cam-5 \
  --url "rtsp://USER:PASS@CAMERA_IP/STREAM" \
  --name "Rear Entrance"
```

3. Restart the edge agent:

```bash
docker restart pilot-edge-agent
```

4. Register topology edges for the new camera. Preferred if an admin API session already exists:

```bash
curl -sS -X POST http://localhost:8080/topology/SITE_UUID/cameras \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=PASTE_ADMIN_COOKIE" \
  -d '{
    "camera_id": "cam-5",
    "name": "Rear Entrance",
    "zone_id": "rear-entrance",
    "latitude": 40.7132,
    "longitude": -74.0048,
    "location_description": "Rear loading entrance"
  }'
```

5. Add at least one topology edge for the new camera:

```bash
curl -sS -X PUT http://localhost:8080/topology/SITE_UUID/edges \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=PASTE_ADMIN_COOKIE" \
  -d '{
    "camera_a_id": "cam-corridor",
    "camera_b_id": "cam-5",
    "transition_time_s": 12,
    "confidence": 0.90,
    "enabled": true
  }'
```

6. Run edge filter calibration:

```bash
python3 scripts/calibration/edge_filter_calibration.py \
  --camera-id cam-5 \
  --edge-config infra/pilot/cameras.yaml \
  --window-s 600
```

### Multi-Node Procedure

1. Update the edge gateway host entry in `infra/ansible/inventory/production.yml` with the new camera under the correct `edge_cameras` list.
2. Re-run the edge-gateway deployment on the affected site and monitoring:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-multi-node.yml \
  --limit edge-site-a.edge.cilex.internal,monitoring-1.core.cilex.internal
```

3. Register the camera and its edges by using the same topology API examples above against the production Query API host.

### Verification

```bash
bash scripts/pilot/list-cameras.sh
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8080/topology/SITE_UUID | python3 -m json.tool | head -60
```

Also verify the camera appears in Grafana Stream Health (`/d/stream-health`) and that new detections are visible through `GET /detections`.

---

## 2. Adding GPU Nodes

### Prerequisites

- Terraform workstation access.
- AMI or image IDs for the chosen cloud, or a bare-metal host already provisioned.
- NVIDIA-compatible GPU and driver support.
- Updated PKI, firewall, and inventory entries.

### Procedure

1. Update Terraform variables for the new Triton node count in `infra/terraform/environments/production/terraform.tfvars`.
2. Apply the infrastructure change:

```bash
cd infra/terraform/environments/production
terraform init
terraform plan
terraform apply
```

3. Add the new host to the `triton` group in `infra/ansible/inventory/production.yml`.
4. Prepare the node and deploy Triton:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-multi-node.yml \
  --limit triton
```

5. On the new node, verify GPU visibility and Triton:

```bash
ssh triton-2.core.cilex.internal 'nvidia-smi -L'
ssh triton-2.core.cilex.internal 'curl -fsS http://localhost:8000/v2/health/ready'
ssh triton-2.core.cilex.internal 'curl -fsS http://localhost:8002/metrics | head -20'
```

6. Load the required models on the new Triton node if EXPLICIT mode is enabled:

```bash
curl -s -X POST http://TRITON_HOST:8000/v2/repository/models/yolov8l/load
curl -s -X POST http://TRITON_HOST:8000/v2/repository/models/osnet/load
curl -s -X POST http://TRITON_HOST:8000/v2/repository/models/color_classifier/load
curl -s -X POST http://TRITON_HOST:8000/v2/repository/models/osnet_reid/load
```

### Verification

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

Check Grafana Inference Performance (`/d/inference-perf`) for lower queue delay and stable VRAM headroom.

---

## 3. Scaling Kafka

### Prerequisites

- Approved maintenance window.
- New broker host provisioned and reachable.
- TLS or SASL material available if running the secured production cluster.

### Procedure

1. Add the new broker host to the `kafka` group in `infra/ansible/inventory/production.yml`.
2. Run the Kafka playbook:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-kafka.yml
```

3. Confirm the new broker is reachable:

```bash
ssh kafka-4.core.cilex.internal 'docker exec kafka-4 /opt/bitnami/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9093 --command-config /opt/bitnami/kafka/config/admin.properties'
```

4. Reassign partitions from the current broker set to the expanded broker set. Example:

```bash
ssh kafka-1.core.cilex.internal '
cat >/tmp/topics-to-move.json <<EOF
{"topics":[
  {"topic":"frames.sampled.refs"},
  {"topic":"frames.decoded.refs"},
  {"topic":"bulk.detections"},
  {"topic":"tracklets.local"},
  {"topic":"events.raw"}
],"version":1}
EOF
docker exec kafka-1 /opt/bitnami/kafka/bin/kafka-reassign-partitions.sh \
  --bootstrap-server localhost:9093 \
  --command-config /opt/bitnami/kafka/config/admin.properties \
  --topics-to-move-json-file /tmp/topics-to-move.json \
  --broker-list 1,2,3,4 \
  --generate > /tmp/reassignment-plan.txt
'
```

5. Review the generated reassignment plan, then execute it.
6. After reassignment finishes, recreate or confirm canonical topics:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/create-kafka-topics.yml
```

### Verification

```bash
ssh kafka-1.core.cilex.internal 'docker exec kafka-1 /opt/bitnami/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9093 --all-groups --describe --command-config /opt/bitnami/kafka/config/admin.properties'
```

Grafana Bus Health (`/d/bus-health`) should show stable consumer lag and no bridge spool growth.

### Stop Condition

Do not continue if any broker is unreachable, consumer lag rises continuously for 10 minutes, or the reassignment plan places all partitions on one broker.

---

## 4. Scaling TimescaleDB

### Supported Paths

- **Recommended first:** Add read replicas for query load.
- **Second:** Tune chunk interval if write volume materially changes.
- **Last resort:** Vertically resize the primary database node.

### Add a Read Replica

1. Provision the replica host through Terraform or your infrastructure platform.
2. Add the host to inventory under a dedicated `timescaledb_replicas` group or your operator inventory notes.
3. Initialize PostgreSQL replication according to your database policy.
4. Point read-only tools or reports to the replica. Keep all writers on the primary.

### Adjust Chunk Interval

Use this only after a sustained change in ingest volume.

```bash
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c \
  "SELECT set_chunk_time_interval('detections', INTERVAL '30 minutes');"

docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c \
  "SELECT set_chunk_time_interval('track_observations', INTERVAL '30 minutes');"
```

### Verification

```bash
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c \
  "SELECT hypertable_name, chunk_time_interval FROM timescaledb_information.dimensions;"
```

Also watch Grafana Storage (`/d/storage`) for:

- `bulk_write_latency_ms`
- `query_latency_ms`
- `bulk_rows_staged`

### Escalate

Escalate to database engineering before changing chunk intervals in production.

---

## 5. Scaling MinIO

### Supported Paths

- **Current supported path:** increase storage on the existing MinIO node.
- **Future path:** distributed MinIO across multiple nodes. This is not yet automated in the repo.

### Expand Existing MinIO Capacity

1. Extend the underlying disk or cloud volume.
2. Extend the filesystem on the MinIO host.
3. Verify free space:

```bash
ssh minio-1.core.cilex.internal 'df -h /var/lib/cilex/minio'
ssh minio-1.core.cilex.internal 'curl -fsS http://localhost:9000/minio/health/live'
```

4. Confirm required buckets still exist:

```bash
docker exec pilot-minio-init mc ls local
```

### Verification

- Grafana Storage (`/d/storage`)
- MinIO console `http://MINIO_HOST:9001`
- Upload and retrieve a test object:

```bash
echo test > /tmp/minio-scale-check.txt
docker run --rm --network host -v /tmp:/tmp minio/mc \
  /bin/sh -c 'mc alias set local http://localhost:9000 minioadmin minioadmin123 && \
              mc cp /tmp/minio-scale-check.txt local/debug-traces/scale-check.txt && \
              mc cat local/debug-traces/scale-check.txt'
```

### Escalate

Escalate to platform engineering if capacity expansion requires changing MinIO topology, not just the backing disk.

---

## 6. Adding Service Instances

### Use This For

- `ingress-bridge`
- `decode-service`
- `inference-worker`
- `attribute-service`
- `event-engine`
- `clip-service`
- `mtmc-service`
- `bulk-collector`
- `query-api`

### Procedure

1. Add another host under the `services` group in `infra/ansible/inventory/production.yml`.
2. Copy the desired `service_deployments` block onto the new host.
3. If the service needs repo-root build context, include:

```yaml
build_context_mode: repo_root
dockerfile: services/<service-name>/Dockerfile
```

4. Deploy the new application node:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-services.yml \
  --limit app-2.core.cilex.internal
```

5. If MTMC is involved, run the dedicated playbook:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-mtmc-infra.yml \
  --limit app-2.core.cilex.internal,monitoring-1.core.cilex.internal
```

### Verification

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

For per-service verification:

- `query-api`: `curl -fsS http://HOST:8000/health`
- `attribute-service`: `curl -fsS http://HOST:8080/metrics | grep attr_classified_total`
- `event-engine`: `curl -fsS http://HOST:8080/metrics | grep event_emitted_total`
- `clip-service`: `curl -fsS http://HOST:8080/metrics | grep clip_extracted_total`
- `mtmc-service`: `curl -fsS http://HOST:8080/metrics | grep mtmc_matches_total`

### Stop Condition

Do not add more application instances if:

- Kafka consumer lag is already rising.
- Query latency is already above target.
- The new host fails the health-check script.

---

## Rollback

If a scaling change causes instability:

1. Stop the new instance or host first.
2. Restore the previous inventory or Terraform plan from `artifacts/scaling-backups/`.
3. Re-run the matching Ansible playbook for the previous state.
4. Re-run:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

5. Keep the failed host or config for forensic review. Do not delete logs or checkpoint files.
