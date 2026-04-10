---
version: "1.0.0"
status: P3-X01
created_by: claude-code
date: "2026-04-10"
---

# Troubleshooting

Common issues grouped by category. Each entry includes symptoms, diagnosis commands, and resolution steps.

For alert-triggered incidents, see `docs/runbooks/incident-response.md` which provides per-alert diagnosis flows.

---

## Installation Issues

### Docker Not Running or Wrong Version

**Symptoms:** `docker: command not found` or `docker compose` fails.

**Diagnosis:**
```bash
docker --version
docker compose version
systemctl status docker
```

**Resolution:**
```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in

# Start Docker if stopped
sudo systemctl start docker
sudo systemctl enable docker
```

### Ansible Connection Failures

**Symptoms:** `UNREACHABLE!` or `Permission denied` during Ansible runs.

**Diagnosis:**
```bash
# Test SSH connectivity
ssh -o BatchMode=yes ubuntu@<target-ip> echo ok

# Test with Ansible ping
ansible -i inventory/mysite.yml all -m ping
```

**Resolution:**
- Verify SSH key is in `~/.ssh/authorized_keys` on target
- Check `ansible_user` in inventory matches the remote user
- Ensure `ansible_python_interpreter` points to an installed Python 3
- For `sudo` errors, verify `ansible_become: true` is set and the user has passwordless sudo

### Terraform State Issues

**Symptoms:** `Error loading state` or `state lock` errors.

**Diagnosis:**
```bash
# Check state lock
terraform force-unlock <lock-id>

# Verify backend configuration
cat backend.hcl
terraform init -backend-config=backend.hcl
```

**Resolution:**
- For stale locks: `terraform force-unlock <lock-id>` (verify no other operator is running)
- For corrupted state: restore from the S3 versioned backup
- For backend access errors: verify AWS credentials and S3/DynamoDB permissions

### Setup Script Fails Mid-Run

**Symptoms:** `setup-pilot.sh` exits with an error partway through.

**Diagnosis:**
```bash
# Check which step failed (the script prints step names)
# Re-run just the failing step manually

# Common: ONNX export fails
python3 scripts/pilot/export_yolov8n_onnx.py

# Common: Docker build fails
cd infra && docker compose -f docker-compose.pilot.yml build --no-cache
```

**Resolution:**
- If ONNX export fails: install `ultralytics` package (`pip install ultralytics`)
- If Docker build fails: check disk space (`df -h`), clear Docker cache (`docker system prune`)
- If container startup fails: check logs (`docker compose -f docker-compose.pilot.yml logs`)

---

## Camera Issues

### RTSP Stream Unreachable

**Symptoms:** Edge agent logs show `Connection refused` or `Stream not found`. No frames appearing in Kafka.

**Diagnosis:**
```bash
# Test RTSP directly from the edge host
ffprobe -v quiet -rtsp_transport tcp -i "rtsp://admin:pass@192.168.1.100/stream1"

# Check edge agent logs
docker logs <edge-agent-container> --tail 50

# Verify camera is reachable
ping 192.168.1.100
```

**Resolution:**
- Verify RTSP URL format matches the camera vendor (see installation guide)
- Check camera credentials (admin password)
- Verify camera is on the correct VLAN and port 554 is not blocked
- Check if camera has reached its maximum connection limit
- Try TCP transport: some cameras default to UDP which may fail through firewalls

### Decode Errors

**Symptoms:** Decode service logs show `Failed to decode frame` or `Unsupported codec`.

**Diagnosis:**
```bash
# Check decode service logs
docker logs <decode-service-container> --tail 50

# Check the frame codec
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
    -of default=nokey=1:noprint_wrappers=1 \
    "rtsp://admin:pass@192.168.1.100/stream1"
```

**Resolution:**
- The decode service supports H.264 and H.265. If the camera outputs a different codec, reconfigure the camera
- For GStreamer errors: verify `gst-plugins-bad` and `gst-plugins-ugly` are installed in the decode container
- For color space issues: the decode service handles BT.601/709 automatically; if colors look wrong, check camera stream settings

### No Motion Detected

**Symptoms:** Edge agent running but no frames published. Kafka topic `frames.sampled.refs` is empty.

**Diagnosis:**
```bash
# Check edge agent metrics
curl -s http://<edge-host>:9090/metrics | grep edge_frames

# Check motion detection settings
cat infra/pilot/cameras.yaml
```

**Resolution:**
- The edge filter passes ~15% of frames on average. If the scene is truly static, no frames are expected
- Lower the motion detection threshold in the edge agent config
- Check if the camera is actually streaming (view RTSP directly with VLC or ffplay)
- After configuration changes, restart the edge agent

---

## Inference Issues

### Triton Not Ready

**Symptoms:** Inference worker logs show `Triton connection refused` or `Model not found`.

**Diagnosis:**
```bash
# Check Triton health
curl -s http://<triton-host>:8000/v2/health/ready

# List loaded models
curl -s http://<triton-host>:8000/v2/models

# Check Triton logs
docker logs <triton-container> --tail 50
```

**Resolution:**
- If Triton is starting: wait for the health check to pass (can take 60+ seconds for GPU model loading)
- If model not found: verify model files exist in the model repository
  ```bash
  ls -la /opt/triton/model-repo/yolov8l/1/
  # Should contain model.plan (GPU) or model.onnx (CPU)
  ```
- For EXPLICIT mode: models must be loaded explicitly
  ```bash
  curl -X POST http://<triton-host>:8000/v2/repository/models/yolov8l/load
  ```

### VRAM Exhaustion

**Symptoms:** Triton logs show `CUDA out of memory`. Models fail to load.

**Diagnosis:**
```bash
# Check GPU memory usage
nvidia-smi

# Check which models are loaded and their memory
curl -s http://<triton-host>:8000/v2/models/stats
```

**Resolution:**
- Normal model set uses ~410 MB of 24 GB VRAM. If VRAM is full:
  - Unload shadow models that are no longer needed
  - Check for runaway dynamic batching (reduce `max_batch_size` in config.pbtxt)
  - Verify no other GPU processes are consuming VRAM (`nvidia-smi`)

### High Inference Latency

**Symptoms:** Detection latency exceeds expected thresholds. Kafka consumer lag on `frames.decoded.refs` is growing.

**Diagnosis:**
```bash
# Check inference worker metrics
curl -s http://<service-host>:9090/metrics | grep inference_latency

# Check GPU utilization
nvidia-smi dmon -s u -d 5  # Sample every 5 seconds

# Check Kafka consumer lag
kafka-consumer-groups.sh --bootstrap-server <kafka>:9093 \
    --describe --group detector-worker
```

**Resolution:**
- If GPU utilization is near 100%: consider adding a GPU node or reducing camera count
- If GPU utilization is low but latency is high: check for CPU bottleneck in NMS or pre/post-processing
- Reduce inference demand:
  - Lower `DECODE__SAMPLER__TARGET_FPS` (e.g., 5.0 -> 2.0)
  - Raise `INFERENCE__DETECTOR__CONFIDENCE_THRESHOLD` (0.35 -> 0.5) to reduce downstream processing
  - Disable cameras that are not needed

---

## Data Flow Issues

### Kafka Consumer Lag Growing

**Symptoms:** Consumer lag on one or more topics increases over time. Data appears delayed.

**Diagnosis:**
```bash
# Check all consumer groups
kafka-consumer-groups.sh --bootstrap-server <kafka>:9093 \
    --describe --all-groups

# Check broker health
kafka-broker-api-versions.sh --bootstrap-server <kafka>:9093
```

**Resolution:**

| Lagging Group | Likely Cause | Fix |
|---------------|-------------|-----|
| `detector-worker` | Inference too slow | Reduce FPS, add GPU, raise confidence threshold |
| `bulk-collector` | DB write bottleneck | Check TimescaleDB connection pool, disk IOPS |
| `attribute-worker` | Triton color model slow | Check GPU utilization, verify model loaded |
| `event-engine` | Event rule evaluation slow | Check event engine logs for specific errors |

### Ingress Bridge Spool Filling

**Symptoms:** Bridge spool directory growing. Spool usage metrics increasing.

**Diagnosis:**
```bash
# Check spool size
du -sh /opt/cilex/bridge-spool/

# Check bridge metrics
curl -s http://<bridge-host>:9091/metrics | grep spool
```

**Resolution:**
- Spool fills when Kafka is unreachable. Check Kafka broker health first
- If Kafka is healthy but bridge can't connect: check SASL credentials, TLS certificates, and firewall rules
- The spool is 50 GB by default; if it fills, the bridge will start dropping old messages
- After Kafka recovery, the bridge replays spooled messages automatically

### Bulk Collector Write Stalls

**Symptoms:** Detections not appearing in TimescaleDB. Collector logs show connection errors or timeouts.

**Diagnosis:**
```bash
# Check collector logs
docker logs <bulk-collector-container> --tail 50

# Check database connectivity
psql -h timescaledb-1 -U cilex -d vidanalytics -c "SELECT 1"

# Check database connection count
psql -h timescaledb-1 -U cilex -d vidanalytics \
    -c "SELECT count(*) FROM pg_stat_activity"
```

**Resolution:**
- If DB unreachable: check TimescaleDB container health, disk space, connection limits
- If connection pool exhausted: increase `max_connections` in PostgreSQL config
- If COPY protocol errors: check for schema mismatches (run `alembic current` to verify migration state)

---

## Storage Issues

### TimescaleDB Write Errors

**Symptoms:** Bulk collector logs show `disk full` or `could not extend file`. Queries fail.

**Diagnosis:**
```bash
# Check disk usage
df -h /var/lib/postgresql/data

# Check chunk sizes
psql -h timescaledb-1 -U cilex -d vidanalytics \
    -c "SELECT * FROM timescaledb_information.chunks ORDER BY range_end DESC LIMIT 10"

# Check compression status
psql -h timescaledb-1 -U cilex -d vidanalytics \
    -c "SELECT * FROM timescaledb_information.compression_settings"
```

**Resolution:**
- If disk full: compression policy triggers after 2 days and achieves 12-15x reduction. Verify compression is running:
  ```sql
  SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression';
  ```
- Manually compress old chunks:
  ```sql
  SELECT compress_chunk(c) FROM show_chunks('detections', older_than => INTERVAL '1 day') c;
  ```
- If retention policy is not dropping old data:
  ```sql
  SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention';
  ```
- Add disk space or extend the volume as a stopgap

### MinIO Full

**Symptoms:** Frame uploads fail. Clip extraction fails. MinIO logs show `disk full`.

**Diagnosis:**
```bash
# Check MinIO disk usage
mc admin info local

# Check bucket sizes
mc du local/frame-blobs
mc du local/event-clips
mc du local/debug-traces
```

**Resolution:**
- Check if lifecycle policies are configured (frame-blobs: 30d, event-clips: 90d, debug-traces: 30d)
- Manually clean old objects:
  ```bash
  mc rm --recursive --older-than 30d local/frame-blobs/
  ```
- Extend the MinIO volume or add a second MinIO node for distributed mode
- For pilot: `rm -rf infra/pilot-data/minio/frame-blobs/.trash/` to reclaim trash space

### FAISS Checkpoint Failures

**Symptoms:** MTMC service logs show `FAISS checkpoint write failed`. Service may lose matching state on restart.

**Diagnosis:**
```bash
# Check FAISS spool directory
du -sh /opt/cilex/faiss-spool/
ls -la /opt/cilex/faiss-spool/

# Check MTMC service logs
docker logs <mtmc-container> --tail 50
```

**Resolution:**
- If spool disk full: extend the volume (100 GB recommended for the FAISS spool)
- If permission error: ensure the container user owns the spool directory
- FAISS checkpoints are optional for crash recovery; the service rebuilds the index from recent embeddings on startup (with a brief matching blackout)

---

## Networking Issues

### mTLS Handshake Failures

**Symptoms:** Edge agent can't connect to NATS. Logs show `TLS handshake error` or `certificate verify failed`.

**Diagnosis:**
```bash
# Test TLS handshake
openssl s_client -connect <nats-host>:4222 \
    -cert /etc/cilex/certs/client.crt \
    -key /etc/cilex/certs/client.key \
    -CAfile /etc/cilex/certs/root_ca.crt

# Check certificate expiry
openssl x509 -in /etc/cilex/certs/client.crt -noout -dates

# Verify cert chain
openssl verify -CAfile /etc/cilex/certs/root_ca.crt /etc/cilex/certs/client.crt
```

**Resolution:**
- **Expired certificates**: Re-run PKI bootstrap or trigger renewal
  ```bash
  bash infra/pki/bootstrap-site.sh --site-id <site-id> --renew
  ```
- **CA mismatch**: Ensure all certs were issued by the same CA. Compare root CA fingerprints:
  ```bash
  openssl x509 -in /etc/cilex/certs/root_ca.crt -noout -fingerprint
  ```
- **Wrong CN**: NATS `verify_and_map` requires specific CN patterns. Check the cert subject:
  ```bash
  openssl x509 -in /etc/cilex/certs/client.crt -noout -subject
  ```

### Clock Drift

**Symptoms:** Cross-camera event correlation is inaccurate. Prometheus alert `ClockDriftWarning` or `ClockDriftCritical` fires.

**Diagnosis:**
```bash
# Check Chrony sync on the affected node
chronyc tracking
chronyc sources -v

# Check drift metric in Prometheus
curl -s 'http://monitoring-1:9090/api/v1/query?query=clock_skew_ms' | python3 -m json.tool
```

**Resolution:**
- If Chrony is not running: `sudo systemctl start chronyd`
- If no NTP sources: verify network access to NTP pools, check firewall rules for UDP port 123
- If drift exceeds 500ms: step the clock manually, then let Chrony maintain it
  ```bash
  sudo chronyc makestep
  ```
- Remember: camera timestamps (`source_capture_ts`) are untrusted. The system relies on `edge_receive_ts` which is set by the Chrony-synced edge host

### NATS Publish Latency

**Symptoms:** Edge agent metrics show high publish latency to NATS. Frame delivery is delayed.

**Diagnosis:**
```bash
# Check NATS server health
curl -s http://<nats-host>:8222/varz | python3 -m json.tool

# Check JetStream status
curl -s http://<nats-host>:8222/jsz | python3 -m json.tool

# Check NATS server logs
docker logs <nats-container> --tail 50
```

**Resolution:**
- If NATS JetStream storage is full (10 GB default): check if consumers are keeping up, increase storage limit
- If network latency between edge and NATS: verify network path, check for packet loss
- If NATS server CPU is high: check for excessive subject subscriptions or slow consumers

---

## Quick Reference: Diagnostic Commands

```bash
# Overall health check
bash scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/mysite.yml

# Container status
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Kafka consumer lag (all groups)
kafka-consumer-groups.sh --bootstrap-server <kafka>:9093 --describe --all-groups

# Triton model status
curl -s http://<triton>:8000/v2/models | python3 -m json.tool

# Database connectivity
psql -h <timescaledb> -U cilex -d vidanalytics -c "SELECT NOW()"

# Recent detection count
psql -h <timescaledb> -U cilex -d vidanalytics \
    -c "SELECT COUNT(*) FROM detections WHERE time > NOW() - INTERVAL '10 minutes'"

# MinIO bucket health
mc admin info local

# GPU status
nvidia-smi

# NTP sync
chronyc tracking

# Certificate expiry
openssl x509 -in /etc/cilex/certs/client.crt -noout -enddate
```
