#!/usr/bin/env bash
# Cilex Vision — CPU-Only Pilot Setup
#
# Checks prerequisites, exports the YOLOv8n ONNX model, starts
# infrastructure, creates Kafka topics, applies DB schema, seeds
# topology, and prints service URLs.
#
# Usage:  bash scripts/pilot/setup-pilot.sh
# Run from the repo root.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.pilot.yml"
ENV_FILE="$REPO_ROOT/infra/pilot/.env.pilot"
MODEL_PATH="$REPO_ROOT/infra/triton/model-repo/yolov8n/1/model.onnx"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ------------------------------------------------------------------
# 1. Prerequisites
# ------------------------------------------------------------------

info "Checking prerequisites..."

command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
docker compose version >/dev/null 2>&1 || fail "Docker Compose (v2) is not installed."
command -v python3 >/dev/null 2>&1 || fail "Python 3 is not installed."

# RAM check
total_ram_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
total_ram_gb=$((total_ram_kb / 1024 / 1024))
if [ "$total_ram_gb" -lt 16 ]; then
    warn "Available RAM: ${total_ram_gb} GB (recommended: 16 GB+)"
else
    info "Available RAM: ${total_ram_gb} GB"
fi

# Disk check
avail_disk_gb=$(df --output=avail "$REPO_ROOT" | tail -1 | awk '{print int($1/1024/1024)}')
if [ "$avail_disk_gb" -lt 50 ]; then
    warn "Available disk: ${avail_disk_gb} GB (recommended: 50 GB+)"
else
    info "Available disk: ${avail_disk_gb} GB"
fi

# ------------------------------------------------------------------
# 2. Export ONNX model
# ------------------------------------------------------------------

if [ -f "$MODEL_PATH" ]; then
    info "YOLOv8n ONNX model already exists: $MODEL_PATH"
else
    info "Exporting YOLOv8n to ONNX (this downloads ~6 MB of weights)..."
    pip install --quiet ultralytics onnx onnxsim 2>/dev/null || true
    python3 "$REPO_ROOT/scripts/pilot/export_yolov8n_onnx.py" --output "$MODEL_PATH"
    info "ONNX model exported."
fi

# ------------------------------------------------------------------
# 3. Copy .env if not present
# ------------------------------------------------------------------

if [ ! -f "$REPO_ROOT/infra/.env" ]; then
    cp "$ENV_FILE" "$REPO_ROOT/infra/.env"
    info "Copied .env.pilot -> infra/.env  (edit credentials before production use)"
else
    info "infra/.env already exists — skipping copy."
fi

# ------------------------------------------------------------------
# 4. Start infrastructure containers
# ------------------------------------------------------------------

info "Starting infrastructure containers..."
cd "$REPO_ROOT/infra"
docker compose -f docker-compose.pilot.yml up -d \
    kafka nats timescaledb minio minio-init redis prometheus grafana triton

info "Waiting for infrastructure to become healthy..."

wait_healthy() {
    local name=$1
    local max_wait=${2:-120}
    local elapsed=0
    while [ $elapsed -lt $max_wait ]; do
        status=$(docker inspect --format='{{.State.Health.Status}}' "pilot-$name" 2>/dev/null || echo "missing")
        if [ "$status" = "healthy" ]; then
            info "  $name: healthy"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    warn "  $name: not healthy after ${max_wait}s (status: $status)"
    return 1
}

wait_healthy kafka 90
wait_healthy nats 60
wait_healthy timescaledb 90
wait_healthy minio 60
wait_healthy minio-init 60
wait_healthy triton 120

# ------------------------------------------------------------------
# 5. Create Kafka topics
# ------------------------------------------------------------------

info "Creating Kafka topics..."
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic frames.sampled.refs --partitions 12 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic frames.decoded.refs --partitions 12 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic tracklets.local --partitions 12 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic bulk.detections --partitions 12 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic mtmc.active_embeddings --partitions 12 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic events.raw --partitions 6 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic attributes.jobs --partitions 6 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic archive.transcode.requested --partitions 4 --replication-factor 1 2>/dev/null || true

docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --create --if-not-exists \
    --topic archive.transcode.completed --partitions 4 --replication-factor 1 2>/dev/null || true

info "Kafka topics created."

# ------------------------------------------------------------------
# 6. Apply DB schema (Alembic migration)
# ------------------------------------------------------------------

info "Applying database schema..."
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "SELECT 1" >/dev/null 2>&1

# Run Alembic migrations via a temporary container
docker run --rm \
    --network cilex-pilot \
    -v "$REPO_ROOT/services/db:/app" \
    -w /app \
    -e DATABASE_URL="postgresql+asyncpg://cilex:cilex_dev_password@pilot-timescaledb:5432/vidanalytics" \
    python:3.11-slim \
    bash -c "pip install --quiet alembic sqlalchemy asyncpg greenlet 2>/dev/null && alembic upgrade head" \
    2>&1 | tail -5

info "Database schema applied."

# ------------------------------------------------------------------
# 7. Seed topology
# ------------------------------------------------------------------

info "Seeding topology data..."
docker run --rm \
    --network cilex-pilot \
    -v "$REPO_ROOT/services/topology:/app" \
    -w /app \
    python:3.11-slim \
    bash -c "pip install --quiet asyncpg pydantic 2>/dev/null && python seed.py --apply --dsn postgresql://cilex:cilex_dev_password@pilot-timescaledb:5432/vidanalytics" \
    2>&1 | tail -3

info "Topology seeded."

# ------------------------------------------------------------------
# 8. Build and start application services
# ------------------------------------------------------------------

info "Building and starting application services..."
docker compose -f docker-compose.pilot.yml up -d --build \
    edge-agent ingress-bridge decode-service inference-worker bulk-collector query-api

info "All services started."

# ------------------------------------------------------------------
# 9. Summary
# ------------------------------------------------------------------

echo ""
echo "=========================================="
echo "  Cilex Vision Pilot — Ready"
echo "=========================================="
echo ""
echo "  Query API:     http://localhost:8080/docs"
echo "  Grafana:        http://localhost:3000    (admin/admin)"
echo "  MinIO Console:  http://localhost:9001    (minioadmin/minioadmin123)"
echo "  Prometheus:     http://localhost:9090"
echo "  Kafka (ext):    localhost:19092"
echo "  NATS:           localhost:4222"
echo ""
echo "  Next steps:"
echo "    1. Edit infra/pilot/cameras.yaml with your camera RTSP URLs"
echo "    2. Restart edge-agent: docker restart pilot-edge-agent"
echo "    3. Check Grafana stream-health dashboard for camera status"
echo "    4. Query detections: curl http://localhost:8080/detections"
echo ""
