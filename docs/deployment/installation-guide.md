---
version: "1.0.0"
status: P3-X01
created_by: claude-code
date: "2026-04-10"
---

# Installation Guide

Step-by-step instructions for deploying Cilex Vision. Choose the path that matches your scenario:

- [Pilot Deployment](#pilot-deployment) -- Single host, CPU-only, 4 cameras, Docker Compose
- [Production Deployment](#production-deployment) -- Multi-node, GPU inference, Ansible + Terraform

## Pilot Deployment

The pilot runs all services on a single machine using Docker Compose with CPU-only inference. Suitable for evaluation with up to 4 cameras.

**Full pilot reference:** `docs/deployment-guide-pilot.md`

### 1. Prepare the Host

Install Ubuntu 24.04 LTS, then install Docker:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for group membership to take effect

# Verify
docker --version          # Should be 24+
docker compose version    # Should be v2.x
```

Install Python 3.11+:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
python3 --version         # Should be 3.11+
```

### 2. Clone the Repository

```bash
git clone <repo-url> cilex-vision
cd cilex-vision
```

### 3. Configure Cameras

Edit `infra/pilot/cameras.yaml` with your RTSP URLs:

```yaml
site_id: pilot-site
cameras:
  - camera_id: cam-1
    rtsp_url: "rtsp://admin:password@192.168.1.100/stream1"
    enabled: true
  - camera_id: cam-2
    rtsp_url: "rtsp://admin:password@192.168.1.101/stream1"
    enabled: true
  - camera_id: cam-3
    rtsp_url: "rtsp://admin:password@192.168.1.102/stream1"
    enabled: true
  - camera_id: cam-4
    rtsp_url: "rtsp://admin:password@192.168.1.103/stream1"
    enabled: true
```

Common RTSP URL patterns by vendor:

| Vendor | URL Pattern |
|--------|-------------|
| Hikvision | `rtsp://admin:pass@IP:554/Streaming/Channels/101` |
| Dahua | `rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0` |
| ONVIF generic | `rtsp://admin:pass@IP:554/stream1` |

Test camera connectivity before proceeding:

```bash
# Test with ffprobe (install via: sudo apt install ffmpeg)
ffprobe -v quiet -rtsp_transport tcp -i "rtsp://admin:pass@192.168.1.100/stream1"
```

### 4. Configure Credentials

```bash
cp infra/pilot/.env.pilot infra/.env
nano infra/.env
```

Change these values from their defaults:

| Variable | Default | Action |
|----------|---------|--------|
| `POSTGRES_PASSWORD` | `cilex_dev_password` | Set a strong password |
| `MINIO_ROOT_PASSWORD` | `minioadmin123` | Set a strong password |
| `JWT_SECRET` | `pilot-jwt-secret-change-me` | Set a random 32+ character string |

### 5. Run the Setup Script

```bash
bash scripts/pilot/setup-pilot.sh
```

The script performs these steps automatically:

1. Checks Docker version, RAM (warns below 16 GB), and disk (warns below 50 GB)
2. Exports YOLOv8n to ONNX format for Triton CPU inference
3. Starts all 15 infrastructure and application containers
4. Creates Kafka topics
5. Applies database schema via Alembic migrations
6. Seeds the 4-camera topology graph
7. Builds and starts application services

Setup takes 5-10 minutes on first run (Docker image pulls + ONNX export).

### 6. Verify the Deployment

```bash
# All containers should show "healthy" or "running"
docker ps --format "table {{.Names}}\t{{.Status}}"

# Check Triton loaded the model
curl -s http://localhost:8001/v2/models/yolov8n | python3 -m json.tool

# Check Query API health
curl -s http://localhost:8080/health

# Check Kafka topics exist
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --list
```

### 7. Access Dashboards

| Service | URL | Default Credentials |
|---------|-----|---------------------|
| Grafana | http://localhost:3000 | admin / admin |
| Query API (Swagger) | http://localhost:8080/docs | JWT required |
| MinIO Console | http://localhost:9001 | minioadmin / minioadmin123 |
| Prometheus | http://localhost:9090 | -- |

### 8. Add or Remove Cameras

```bash
# Add a camera
bash scripts/pilot/add-camera.sh \
    --id cam-5 \
    --url "rtsp://admin:pass@192.168.1.104/stream1" \
    --name "Rear Entrance"

# List cameras
bash scripts/pilot/list-cameras.sh

# Restart edge agent to pick up changes
docker restart pilot-edge-agent
```

For detailed camera onboarding, see `docs/runbooks/camera-onboarding.md`.

---

## Production Deployment

Multi-node deployment using Ansible for orchestration and optionally Terraform for infrastructure provisioning. Supports GPU inference with the full model set (YOLOv8-L, OSNet, color classifier).

### Prerequisites

On the **operator workstation** (the machine running Ansible):

```bash
# Python 3.11+
python3 --version

# Ansible 2.14+
pip install ansible>=2.14
ansible --version

# Terraform 1.5+ (if provisioning cloud infrastructure)
terraform --version

# SSH access to all target nodes
ssh -o BatchMode=yes ubuntu@<target-ip> echo ok
```

On all **target nodes**:

- Ubuntu 24.04 LTS
- Python 3 installed (`apt install python3`)
- SSH access from operator workstation (key-based auth recommended)
- Docker 24+ (installed by the Ansible `common` role, or pre-install)

### 1. Clone and Configure

```bash
git clone <repo-url> cilex-vision
cd cilex-vision
```

### 2. Prepare the Inventory

Copy the production inventory template:

```bash
cp infra/ansible/inventory/production.yml infra/ansible/inventory/mysite.yml
```

Edit `infra/ansible/inventory/mysite.yml` with your actual host IPs and credentials. The inventory defines these host groups:

| Group | Purpose | Example Hosts |
|-------|---------|---------------|
| `kafka` | Kafka brokers (3 for production) | kafka-1, kafka-2, kafka-3 |
| `timescaledb` | TimescaleDB database | timescaledb-1 |
| `minio` | MinIO object storage | minio-1 |
| `triton` | GPU inference nodes | triton-1, triton-2 |
| `monitoring` | Prometheus + Grafana | monitoring-1 |
| `mlflow` | MLflow experiment tracking | mlflow-1 |
| `services` | Application services | app-1, app-2 |
| `edge_gateways` | Edge agent + NATS per site | edge-site-a, edge-site-b |
| `nats` | NATS JetStream per site | nats-site-a, nats-site-b |
| `cvat` | Annotation tool (optional) | -- |

Key variables to set per host:

```yaml
# Kafka nodes
kafka_node_id: 1
kafka_ssl_keystore_src: /secure/kafka/kafka-1/broker.keystore.p12
kafka_ssl_truststore_src: /secure/kafka/kafka-1/broker.truststore.p12

# Triton nodes
triton_gpu_devices: ["0"]

# Edge gateways
edge_site_id: site-a
edge_nats_url: tls://10.42.20.11:4222
edge_cameras:
  - camera_id: cam-site-a-001
    rtsp_url: rtsp://admin:pass@192.168.1.100/stream1
    enabled: true
```

Set all `replace-me` passwords in the inventory's `all.vars` section.

### 3. Bootstrap PKI

The internal PKI issues mTLS certificates for NATS edge-to-core communication.

```bash
# Bootstrap the CA and issue initial certificates
bash infra/pki/bootstrap-site.sh --site-id site-01

# For each additional site
bash infra/pki/bootstrap-site.sh --site-id site-02
```

This creates:

- Root CA and online intermediate CA (step-ca)
- NATS server certificates per site
- Edge client certificates per site
- CA root bundle for distribution

Certificates are valid for 90 days and auto-renew via the `internal-acme` provisioner.

### 4. Provision Infrastructure (Cloud Only)

Skip this step for bare-metal deployments.

```bash
cd infra/terraform/environments/production

# Configure backend for state storage
cat > backend.hcl <<EOF
bucket         = "cilex-tf-state"
key            = "production/terraform.tfstate"
region         = "us-east-1"
dynamodb_table = "cilex-tf-locks"
EOF

# Initialize
terraform init -backend-config=backend.hcl

# Review the plan
terraform plan -var-file=terraform.tfvars

# Apply
terraform apply -var-file=terraform.tfvars
```

Update `terraform.tfvars` with:

- `deployment_provider`: `aws`, `gcp`, or `bare_metal`
- AMI/image IDs for your region
- SSH key pair name
- Desired instance types

After provisioning, update the Ansible inventory with the output IPs:

```bash
terraform output -json > /tmp/tf-outputs.json
# Manually update inventory/mysite.yml with the output IPs
```

### 5. Deploy with Ansible

#### Option A: Full deployment (recommended for first install)

```bash
cd infra/ansible
ansible-playbook -i inventory/mysite.yml playbooks/deploy-multi-node.yml
```

This runs in dependency order:

1. Prepare GPU nodes (NVIDIA drivers, Docker GPU runtime)
2. Deploy TimescaleDB
3. Deploy MinIO (with bucket creation)
4. Deploy NATS (per-site, with mTLS)
5. Deploy Kafka (3-broker cluster with SASL_SSL)
6. Create Kafka topics
7. Deploy Triton (with model repository sync)
8. Deploy edge gateways (GStreamer, PKI, edge agent)
9. Deploy application services
10. Deploy MTMC infrastructure
11. Deploy monitoring (Prometheus, Grafana)
12. Deploy MLflow
13. Run smoke tests

#### Option B: Deploy individual services

```bash
# Deploy only TimescaleDB
ansible-playbook -i inventory/mysite.yml playbooks/deploy-timescaledb.yml

# Deploy only Kafka
ansible-playbook -i inventory/mysite.yml playbooks/deploy-kafka.yml

# Deploy only application services
ansible-playbook -i inventory/mysite.yml playbooks/deploy-services.yml
```

### 6. Load Triton Models

Triton runs in EXPLICIT mode (ADR-005). Models must be explicitly loaded after deployment.

Verify models are in the repository:

```bash
# SSH to a Triton node
ssh ubuntu@triton-1

# Check model repository
ls -la /opt/triton/model-repo/
# Should contain: yolov8l/, osnet/, color_classifier/
# Each with: config.pbtxt and 1/model.plan (TensorRT engine)
```

The Ansible `deploy-triton.yml` playbook syncs the model repository. If models need manual loading:

```bash
# Load a model via Triton HTTP API
curl -X POST http://triton-1:8000/v2/repository/models/yolov8l/load
curl -X POST http://triton-1:8000/v2/repository/models/osnet/load
curl -X POST http://triton-1:8000/v2/repository/models/color_classifier/load

# Verify all models are ready
curl -s http://triton-1:8000/v2/models/yolov8l | python3 -m json.tool
curl -s http://triton-1:8000/v2/models/osnet | python3 -m json.tool
curl -s http://triton-1:8000/v2/models/color_classifier | python3 -m json.tool
```

### 7. Run Database Migrations

Migrations run automatically during Ansible deployment. To run manually:

```bash
# From the operator workstation or a service node
cd services/db
DATABASE_URL="postgresql+asyncpg://cilex:<password>@timescaledb-1:5432/vidanalytics" \
    alembic upgrade head
```

### 8. Verify the Deployment

Run the health check script:

```bash
bash scripts/deploy/health-check-all.sh \
    --inventory infra/ansible/inventory/mysite.yml
```

This checks:

- Kafka broker connectivity and topic list
- NATS server health (per site)
- TimescaleDB readiness
- MinIO health and bucket existence
- Triton model readiness
- Prometheus scrape targets
- Grafana health
- Edge agent metrics endpoints
- Application service health endpoints

The script prints a PASS/FAIL table and exits non-zero on any failure.

### 9. Onboard Cameras

After infrastructure is verified, add cameras to each site:

```bash
# Reference the camera onboarding runbook
cat docs/runbooks/camera-onboarding.md
```

Steps per camera:

1. Add camera to the edge gateway's camera config
2. Register in the topology graph via the admin API
3. Restart the edge agent to pick up the new camera
4. Verify the stream appears in the Grafana Stream Health dashboard
5. Run edge filter calibration:

```bash
python scripts/calibration/edge_filter_calibration.py \
    --camera-id cam-site-a-001 \
    --dsn "postgresql://cilex:<password>@timescaledb-1:5432/vidanalytics" \
    --output-dir artifacts/calibration/cam-site-a-001/
```

### 10. Access Dashboards

| Service | URL | Default Credentials |
|---------|-----|---------------------|
| Grafana | http://monitoring-1:3000 | admin / (set in inventory) |
| Query API (Swagger) | http://app-1:8000/docs | JWT required |
| MinIO Console | http://minio-1:9001 | (set in inventory) |
| Prometheus | http://monitoring-1:9090 | -- |
| MLflow | http://mlflow-1:5000 | -- |

---

## Post-Installation

### Verify Data Flow

After cameras are onboarded, confirm the full pipeline:

1. **Edge agent**: Check logs for RTSP connection and frame publishing

   ```bash
   docker logs <edge-agent-container> --tail 20
   ```

2. **Kafka**: Verify messages flowing through topics

   ```bash
   # On a Kafka broker
   kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
       --describe --group detector-worker
   ```

3. **TimescaleDB**: Query for recent detections

   ```bash
   psql -h timescaledb-1 -U cilex -d vidanalytics \
       -c "SELECT COUNT(*) FROM detections WHERE time > NOW() - INTERVAL '5 minutes'"
   ```

4. **Query API**: Search for detections

   ```bash
   curl -s http://app-1:8000/detections?limit=5 | python3 -m json.tool
   ```

### Set Up Monitoring Alerts

Grafana comes pre-provisioned with dashboards. Verify:

- **Stream Health**: All cameras show connected status
- **Inference Performance**: Detection latency within expected range
- **Bus Health**: Kafka consumer lag near zero
- **Storage**: Disk usage within safe bounds
- **Model Quality**: Detection confidence distribution normal

### Schedule Backups

Set up regular backups following `docs/runbooks/backup-restore.md`:

```bash
# Example: daily TimescaleDB backup via cron
0 2 * * * /opt/cilex/scripts/backup-timescaledb.sh >> /var/log/cilex-backup.log 2>&1
```
