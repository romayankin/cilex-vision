---
version: "1.0.0"
status: P3-X01
created_by: claude-code
date: "2026-04-10"
---

# Cilex Vision Deployment Guide

Cilex Vision is a multi-camera video analytics platform that performs real-time object detection, single-camera tracking, cross-camera Re-ID, event generation, and clip extraction. It processes RTSP streams at the edge, transports metadata through a dual message bus (NATS at edge, Kafka at core), and stores results in TimescaleDB and MinIO for search and retrieval through a REST API and web frontend.

## Deployment Scenarios

| Scenario | Cameras | GPU | Hosts | Use Case |
|----------|---------|-----|-------|----------|
| **Pilot** | 4 | None (CPU inference) | 1 | Evaluation, proof-of-concept, development |
| **Small** | up to 10 | 1x 24 GB | 2 | Single-site production with one GPU |
| **Medium** | up to 50 | 2x 24 GB | 10-12 | Multi-site production |
| **Large** | 100+ | 3+ x 24 GB | 15+ | Multi-site, horizontally scaled |

See [Hardware Requirements](hardware-requirements.md) for detailed per-scenario sizing.

## Prerequisites

| Software | Version | Purpose |
|----------|---------|---------|
| Docker | 24+ | Container runtime (all scenarios) |
| Docker Compose | v2 (bundled) | Single-host orchestration (pilot) |
| Python | 3.11+ | Setup scripts, calibration, migrations |
| Git | any | Clone repository |
| Ansible | 2.14+ | Multi-node deployment (small/medium/large) |
| Terraform | 1.5+ | Cloud infrastructure provisioning (optional) |

## Documentation

| Document | Contents |
|----------|----------|
| [Hardware Requirements](hardware-requirements.md) | CPU, RAM, disk, GPU, and network sizing per scenario |
| [Installation Guide](installation-guide.md) | Step-by-step deployment for pilot and production |
| [Network Guide](network-guide.md) | Camera VLAN, firewall rules, mTLS, DNS, NTP, bandwidth planning |
| [Upgrade Guide](upgrade-guide.md) | Rolling upgrades, database migrations, model updates, rollback |
| [Troubleshooting](troubleshooting.md) | Common issues with symptoms, diagnosis commands, and resolution |

## Quick Start (Pilot)

```bash
# 1. Clone the repository
git clone <repo-url> cilex-vision && cd cilex-vision

# 2. Configure camera RTSP URLs
nano infra/pilot/cameras.yaml

# 3. Set credentials
cp infra/pilot/.env.pilot infra/.env && nano infra/.env

# 4. Run setup (downloads model, starts all 15 containers)
bash scripts/pilot/setup-pilot.sh

# 5. Verify
docker ps --format "table {{.Names}}\t{{.Status}}"
```

For detailed pilot instructions, see [Installation Guide - Pilot Deployment](installation-guide.md#pilot-deployment).

## Production Quick Start

```bash
# 1. Clone and configure inventory
git clone <repo-url> cilex-vision && cd cilex-vision
cp infra/ansible/inventory/production.yml infra/ansible/inventory/mysite.yml
nano infra/ansible/inventory/mysite.yml

# 2. Bootstrap PKI
bash infra/pki/bootstrap-site.sh --site-id site-01

# 3. Provision infrastructure (cloud only)
cd infra/terraform/environments/production
terraform init && terraform apply

# 4. Deploy all services
cd infra/ansible
ansible-playbook -i inventory/mysite.yml playbooks/deploy-multi-node.yml

# 5. Verify
bash scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/mysite.yml
```

For detailed production instructions, see [Installation Guide - Production Deployment](installation-guide.md#production-deployment).

## Deployment Order

Services must be deployed in dependency order. The Ansible playbooks handle this automatically:

1. **TimescaleDB** -- Database must be ready for schema migrations
2. **MinIO** -- Object storage for frames, clips, thumbnails
3. **NATS** -- Edge message broker (per-site)
4. **Kafka + Topics** -- Central message bus and topic creation
5. **Triton** -- Inference server with model repository
6. **Application Services** -- Edge agent, ingress bridge, decode, inference, bulk collector, query API, attribute, event engine, clip, MTMC
7. **Monitoring** -- Prometheus, Grafana, alerting rules
8. **MLflow** -- Experiment tracking (optional for production)
9. **CVAT** -- Annotation tool (optional)
10. **Smoke Test** -- End-to-end verification

## Operational Runbooks

After deployment, refer to these runbooks for ongoing operations:

| Runbook | Purpose |
|---------|---------|
| [Incident Response](../runbooks/incident-response.md) | Per-alert diagnosis and resolution |
| [Scaling](../runbooks/scaling.md) | Adding cameras, GPU nodes, Kafka brokers, DB capacity |
| [Backup & Restore](../runbooks/backup-restore.md) | Backup procedures with RPO/RTO targets |
| [Camera Onboarding](../runbooks/camera-onboarding.md) | Step-by-step camera addition |
| [Service Restart](../runbooks/service-restart.md) | Dependency-ordered restart procedures |
| [Model Rollout](../runbooks/model-rollout-sop.md) | Shadow deployment and cutover lifecycle |

## Architecture Overview

```
Cameras (RTSP)
  |
  v
Edge Agent --> NATS JetStream --> Ingress Bridge --> Kafka
                                                      |
              +---------------------------------------+
              |               |              |
        frames.sampled   tracklets      events
              |               |              |
        Decode Service   Attribute Svc   Event Engine
              |               |              |
        frames.decoded   Bulk Collector  Clip Service
              |               |              |
        Inference Worker     MTMC       MinIO (clips)
         (Triton GPU)        |
           /     \       TimescaleDB
    detections  tracklets     |
         |                Query API --> Frontend
    Bulk Collector              |
         |                 Grafana
    TimescaleDB
```

## Support

- Repository issues: Report bugs and feature requests in the project issue tracker
- Runbooks: Check `docs/runbooks/` for operational procedures
- Monitoring: Access Grafana dashboards for real-time system health
