# Capacity Planning

Resource inventory, scaling indicators, and growth projections for the Cilex Vision platform.

---

## Current Resource Inventory

### Cameras

| Parameter | Pilot | Production target |
|-----------|-------|-------------------|
| Cameras per site | 4 | 10-100+ |
| Effective FPS per camera (after motion filter) | ~2-5 | ~2-5 |
| Motion filter pass-through rate | ~15% | ~15% |

### GPU

| Parameter | Pilot | Production |
|-----------|-------|-----------|
| GPU count | 0 (CPU-only) or 1 | 1+ per ~25 cameras |
| GPU model | NVIDIA with 24GB VRAM | NVIDIA T4/A10/A100 |
| VRAM per model set | ~600MB (3 models) | ~600MB |
| VRAM headroom | ~97% free on 24GB | Maintain >20% free |

**Models loaded per GPU:**

| Model | VRAM | Purpose |
|-------|------|---------|
| yolov8l (YOLOv8-Large) | ~185MB | Object detection (7 classes) |
| osnet (OSNet-x1.0) | ~185MB | Re-ID embeddings (512-dim) |
| color_classifier (ResNet-18) | ~185MB | Color attribute classification |

During shadow deployment, a second model set is loaded alongside production. Budget ~1.2GB VRAM total when shadow is active.

### Storage

| Component | Pilot capacity | What it stores |
|-----------|---------------|----------------|
| TimescaleDB | Single-node, volume-backed | Detections, track observations, events, metadata |
| MinIO | Single-node, volume-backed | Frame blobs, event clips, thumbnails, debug traces |
| Kafka | 3-broker cluster | Message queue for all pipeline topics |

### Infrastructure Services

| Service | Pilot | Production |
|---------|-------|-----------|
| Kafka brokers | 3 (docker-compose) | 3+ (Ansible-managed) |
| TimescaleDB | 1 primary | 1 primary + read replicas |
| MinIO | 1 node | 1 node (expandable) |
| NATS | 1 per edge site | 1 per edge site |
| Redis | 1 | 1 |
| Prometheus | 1 | 1 |
| Grafana | 1 | 1 |

---

## Scaling Indicators

### When to Add Cameras

Refer to [Camera Onboarding Runbook](../runbooks/camera-onboarding.md) for the onboarding procedure.

**Pre-check before adding cameras:**

- Inference worker consumer lag is near zero.
- GPU VRAM headroom is above 20%.
- TimescaleDB write latency p99 is below 50ms.
- Kafka consumer lag across all groups is stable.

### When to Add GPU Nodes

**Indicators that GPU capacity is needed:**

| Signal | Dashboard | Threshold |
|--------|-----------|-----------|
| Inference consumer lag growing | Inference Performance | Lag >1000 sustained for >10 min |
| Triton queue delay elevated | Inference Performance | p95 >50ms sustained |
| VRAM usage high | Inference Performance | >80% (>70% with shadow active) |
| Frames consumed/sec dropping | Inference Performance | Below expected rate for camera count |

**Rule of thumb:** One 24GB GPU supports approximately 25 cameras at 5 FPS effective (after motion filtering), with all 3 models loaded and 20% VRAM headroom.

**Procedure:** [Scaling Runbook — Adding GPU Nodes](../runbooks/scaling.md)

### When to Add Storage

**TimescaleDB indicators:**

| Signal | Dashboard | Threshold |
|--------|-----------|-----------|
| Write latency p99 rising | Storage | Sustained >100ms |
| Staged rows growing | Storage | Sustained >50,000 |
| Disk usage high | (host monitoring) | >80% |

**MinIO indicators:**

| Signal | Dashboard | Threshold |
|--------|-----------|-----------|
| Disk usage high | Storage Tiering | >70% (`MinIODiskUsageHigh`) |
| Lifecycle expiration stalled | Storage Tiering | No expirations for >24h |
| Bucket capacity warning | Storage Tiering | Per-bucket threshold exceeded |

**Procedure:** [Scaling Runbook — TimescaleDB and MinIO](../runbooks/scaling.md)

### When to Add Kafka Brokers

**Indicators:**

| Signal | Dashboard | Threshold |
|--------|-----------|-----------|
| Consumer lag rising across groups | Bus Health | Growing for >10 min |
| Produce latency elevated | Bus Health | p95 >100ms |
| Broker disk usage | (host monitoring) | >80% |

**Procedure:** [Scaling Runbook — Scaling Kafka](../runbooks/scaling.md)

---

## Per-Camera Resource Estimates

These are approximate resource requirements per camera, assuming ~15% motion filter pass-through and ~5 FPS effective input to the inference pipeline.

### Compute

| Resource | Per camera | Notes |
|----------|-----------|-------|
| GPU inference time | ~40ms/frame | YOLOv8-L + OSNet + color classifier |
| GPU throughput capacity | ~25 FPS | Per 24GB GPU with dynamic batching |
| CPU (inference worker) | ~0.1 core | NMS, tracking, publishing |
| CPU (edge agent) | ~0.2 core | GStreamer decode, motion detection |
| CPU (decode service) | ~0.1 core | Central decode and color space normalization |
| Memory (edge agent) | ~200MB | Per camera stream buffer |

### Storage Growth

| Data type | Per camera per day | Retention | Total per camera |
|-----------|--------------------|-----------|-----------------|
| Detection rows | ~200K-500K rows | 30 days | ~6M-15M rows |
| Track observation rows | ~50K-100K rows | 30 days | ~1.5M-3M rows |
| Frame blobs (MinIO) | ~2-5 GB | 7 days | ~14-35 GB |
| Event clips (MinIO) | ~0.5-2 GB | 90 days | ~45-180 GB |
| Thumbnails (MinIO) | ~100-500 MB | 30 days | ~3-15 GB |
| Debug traces (MinIO) | ~50-200 MB | 30 days | ~1.5-6 GB |

### Network

| Path | Per camera | Notes |
|------|-----------|-------|
| RTSP to edge agent | 2-8 Mbps | Depends on resolution and codec |
| Edge to center (after filter) | ~0.3-1.2 Mbps | ~15% of raw, NATS + frame refs |
| Kafka internal | ~0.5-1 Mbps | Detection/tracklet/event messages |

---

## Growth Projections

### Example: 4-Camera Pilot to 20-Camera Site

| Resource | 4 cameras (pilot) | 20 cameras | Action needed |
|----------|-------------------|------------|---------------|
| GPU | 0 (CPU) or 1 | 1 (24GB) | Add GPU if pilot is CPU-only |
| TimescaleDB rows/day | ~1-2M | ~5-10M | Monitor write latency |
| MinIO storage/day | ~10-30 GB | ~50-150 GB | Expand MinIO volume |
| Kafka throughput | ~50-200 msg/s | ~250-1000 msg/s | 3 brokers sufficient |
| Edge agents | 1 | 1-2 (by site) | One per physical site |

### Example: 20-Camera Site to 100-Camera Multi-Site

| Resource | 20 cameras | 100 cameras | Action needed |
|----------|-----------|-------------|---------------|
| GPU | 1 | 4 | Add GPU nodes via Terraform |
| TimescaleDB | 1 primary | 1 primary + 2 read replicas | Scale reads, monitor writes |
| MinIO | 1 node ~150 GB/day | 1 node ~750 GB/day | Expand disk or add distributed MinIO |
| Kafka | 3 brokers | 3-5 brokers | Add broker if lag grows |
| Edge agents | 1-2 | 5-10 (one per site) | Deploy per site via Ansible |

---

## Monitoring Capacity Itself

Use these dashboards to track whether the monitoring infrastructure is keeping up:

- **Prometheus:** Check Prometheus self-metrics at `/metrics`. Watch `prometheus_tsdb_head_series` for cardinality growth.
- **Grafana:** Check Grafana health at `/api/health`. Slow dashboard loads indicate query pressure.
- **Node exporter:** Monitor host CPU, memory, and disk on all nodes.

**Rule of thumb:** Prometheus resource usage grows linearly with the number of scraped targets (cameras x services x metrics). Plan for approximately 1000 time series per camera.
