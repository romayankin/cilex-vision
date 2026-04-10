---
version: "1.0.0"
status: P3-X01
created_by: claude-code
date: "2026-04-10"
---

# Hardware Requirements

This document specifies hardware requirements for each Cilex Vision deployment scenario. All specs assume 1080p H.264 camera streams at 25 FPS with ~15% motion duty cycle (edge filter pass-through rate).

## Pilot (4 Cameras)

Single-host deployment using Docker Compose. CPU-only inference with YOLOv8n.

| Resource | Specification |
|----------|--------------|
| **CPU** | 12+ threads (e.g., Intel i5-13500, AMD Ryzen 5 5600X) |
| **RAM** | 16 GB minimum |
| **Disk** | 50 GB SSD |
| **GPU** | Not required |
| **Network** | 1 Gbps NIC, access to camera VLAN |
| **OS** | Ubuntu 24.04 LTS |

**Container memory budget:**

| Container | Memory Limit |
|-----------|-------------|
| Kafka (single KRaft) | 1.5 GB |
| TimescaleDB | 2 GB |
| Triton (CPU, YOLOv8n) | 2 GB |
| MinIO | 1 GB |
| Prometheus | 512 MB |
| NATS | 256 MB |
| Redis | 256 MB |
| Grafana | 256 MB |
| Application services (7) | 512 MB each |
| **Total** | ~11 GB |

**Performance expectations (Intel i5-13500, YOLOv8n CPU ONNX):**

| Metric | Value |
|--------|-------|
| Inference latency per frame | 80-120 ms |
| Inference throughput | ~10 FPS |
| 4 cameras at 15% duty cycle | ~3 FPS demand |
| Steady-state CPU utilization | 40-60% |

## Small (10 Cameras)

Single GPU host with one edge gateway per remote site.

### GPU Host

| Resource | Specification |
|----------|--------------|
| **CPU** | 16+ threads (e.g., Intel i7-13700, AMD Ryzen 7 5800X) |
| **RAM** | 32 GB |
| **Disk** | 200 GB NVMe SSD |
| **GPU** | 1x NVIDIA with 24 GB VRAM (A5000, RTX 4090, L40) |
| **Network** | 1 Gbps NIC |
| **OS** | Ubuntu 24.04 LTS |

Runs all core services (Kafka single-node, TimescaleDB, MinIO, Triton, application services, monitoring) on one machine.

### Edge Gateway (1 per remote site)

| Resource | Specification |
|----------|--------------|
| **CPU** | 4+ threads |
| **RAM** | 8 GB |
| **Disk** | 50 GB SSD (NVMe spool for bridge) |
| **GPU** | Not required |
| **Network** | 1 Gbps NIC on camera VLAN + WAN uplink to core |

Runs edge agent, NATS JetStream, and ingress bridge.

### GPU Memory Budget

All three models run on a single 24 GB GPU with FP16 TensorRT engines:

| Model | Role | VRAM (FP16) |
|-------|------|-------------|
| YOLOv8-L | Object detection (7 classes) | ~190 MB |
| OSNet x1.0 | Re-ID embeddings (512-d) | ~110 MB |
| ResNet-18 | Color classification (10 colors) | ~110 MB |
| **Total** | | **~410 MB (~1.7% of 24 GB)** |

Remaining VRAM is available for shadow model deployment (ADR-005) and dynamic batching buffers.

## Medium (50 Cameras)

Multi-node deployment with dedicated service roles. Provisioned via Terraform and deployed with Ansible.

### Node Inventory

| Role | Count | CPU | RAM | Disk | GPU | Purpose |
|------|-------|-----|-----|------|-----|---------|
| Kafka broker | 3 | 8 threads | 16 GB | 500 GB NVMe | -- | Central message bus (RF=3) |
| TimescaleDB | 1 | 16 threads | 64 GB | 1 TB NVMe | -- | Detection/track time-series + relational metadata |
| MinIO | 1 | 8 threads | 16 GB | 2 TB HDD | -- | Object storage (frames, clips, thumbnails) |
| GPU node | 2 | 16 threads | 32 GB | 200 GB SSD | 1x 24 GB | Triton inference (active + shadow) |
| Service node | 2 | 16 threads | 32 GB | 200 GB SSD | -- | Application services (query API, event engine, MTMC, etc.) |
| Monitoring | 1 | 8 threads | 16 GB | 500 GB SSD | -- | Prometheus, Grafana, alerting |
| MLflow | 1 | 4 threads | 8 GB | 100 GB SSD | -- | Experiment tracking, model registry |
| Edge gateway | N (1/site) | 4 threads | 8 GB | 50 GB SSD | -- | Edge agent + NATS + bridge |

**Total core nodes:** 11 + N edge gateways

### Why 2 GPU Nodes

- One GPU runs the active model versions
- Second GPU runs shadow deployments during model rollout (ADR-005: 24-48 hour comparison window)
- Either GPU can serve as failover if one node goes down
- At 50 cameras with 15% duty cycle, sustained inference demand is ~19 FPS -- well within a single GPU's capacity (~200 FPS for YOLOv8-L on A5000)

## Large (100+ Cameras)

Horizontally scaled from the medium topology.

### Scaling from Medium

| Role | Medium | Large | Scaling Notes |
|------|--------|-------|---------------|
| Kafka broker | 3 | 3 | 3 brokers handle 100+ cameras; add brokers only if throughput exceeds 500 MB/s |
| TimescaleDB | 1 | 1 + read replica | Add read replica for query API load; write node handles COPY ingest |
| MinIO | 1 | 2-4 (distributed) | Switch to distributed MinIO for redundancy and throughput |
| GPU node | 2 | 3+ | Add GPU nodes when inference demand exceeds ~180 FPS sustained |
| Service node | 2 | 3+ | Add nodes for horizontal scaling of stateless services |
| Monitoring | 1 | 1 | Single Prometheus with longer retention or Thanos for federation |
| Edge gateway | N | N | 1 per site, unchanged |

### When to Add GPU Nodes

Inference demand formula:

```
FPS_demand = num_cameras x source_fps x motion_duty_cycle x decode_sample_rate
```

Example for 100 cameras:

```
100 cameras x 25 FPS x 0.15 duty x 0.2 sample = 75 FPS demand
```

A single A5000 sustains ~200 FPS for YOLOv8-L, so 100 cameras fits on one active GPU. Add a second active GPU at ~150+ cameras or when shadow deployment is active.

## Disk IOPS Guidance

| Service | IOPS Requirement | Recommendation |
|---------|-----------------|----------------|
| Kafka brokers | High sequential write, moderate random read | NVMe SSD, 3000+ IOPS |
| TimescaleDB | High sequential write (COPY), moderate random read (queries) | NVMe SSD, 5000+ IOPS |
| MinIO | Moderate sequential write, moderate random read | HDD acceptable for medium; NVMe for large |
| Prometheus | Moderate sequential write | SSD, 1000+ IOPS |
| Edge spool (ingress bridge) | Burst sequential write during Kafka outages | NVMe SSD, 50 GB reserved |

## Network Bandwidth

### Per-Camera Bandwidth

| Stream | Bandwidth |
|--------|-----------|
| 1080p H.264 @ 25 FPS (main stream) | 4-8 Mbps |
| 720p H.264 @ 15 FPS (sub stream) | 1-3 Mbps |
| **Planning estimate** | **5 Mbps per camera** |

### Edge-to-Core Bandwidth

After edge filtering (~15% pass-through), the ingress bridge forwards only sampled frame references and metadata:

| Data | Per-Camera Bandwidth |
|------|---------------------|
| Frame blob upload to MinIO | ~200 Kbps (JPEG, 15% of frames) |
| Kafka message metadata | ~10 Kbps |
| **Total edge-to-core** | **~250 Kbps per camera** |

### Aggregate Bandwidth Planning

| Scenario | Cameras | Camera VLAN | Edge-to-Core WAN |
|----------|---------|-------------|------------------|
| Pilot | 4 | 20 Mbps | N/A (single host) |
| Small | 10 | 50 Mbps | 2.5 Mbps per site |
| Medium | 50 | 250 Mbps total | 12.5 Mbps total |
| Large | 100 | 500 Mbps total | 25 Mbps total |

### Core Internal Network

All core nodes should be connected via 1 Gbps minimum. 10 Gbps recommended for medium/large deployments, especially between:

- Kafka brokers (replication traffic)
- TimescaleDB and service nodes (COPY ingest, query results)
- MinIO and decode/clip services (frame blob transfer)
