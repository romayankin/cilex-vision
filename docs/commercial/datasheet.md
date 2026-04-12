# Cilex Vision Datasheet

This datasheet is intended for procurement, IT, and operational stakeholders evaluating fit, deployment requirements, and scale assumptions.

## Platform Summary

| Category | Specification |
|---|---|
| Product type | Multi-camera video analytics platform |
| Primary outputs | Detections, tracks, events, clips, thumbnails, optional plate reads |
| Object classes | person, car, truck, bus, bicycle, motorcycle, animal |
| Core deployment models | Single-site, multi-site, hybrid edge-to-core |
| Scale target | Up to 100 cameras per deployment |

## Supported Inputs and Integrations

| Area | Support |
|---|---|
| Camera protocols | RTSP for video ingest, ONVIF for discovery and capability checks |
| Typical video planning baseline | 1080p H.264 at 25 FPS |
| Camera compatibility model | Vendor-agnostic IP cameras with RTSP and ONVIF support |
| Search and retrieval | REST API and web application |
| Operational evidence | Clips, thumbnails, event-linked assets |
| Annotation workflow | CVAT-based model improvement workflow |
| Platform-native message contracts | Protobuf messages over Kafka topics |

## Functional Capabilities

| Capability | Current scope |
|---|---|
| Detection | 7 object classes with tunable confidence thresholds |
| Tracking | Real-time multi-object tracking with track lifecycle management |
| Attributes | vehicle color, person upper color, person lower color |
| Events | entered scene, exited scene, stopped vehicle, loitering, motion started, motion ended |
| Cross-camera Re-ID | Site-level identity linking using visual similarity matching |
| LPR | Two-stage plate detection and OCR with quality gating |
| Search | Filtered search across detections, tracks, and events |
| Investigation | Clip generation, thumbnails, and evidence retrieval |
| Multi-site operations | Site dashboard, site management, cross-site comparison |
| Edge resilience | Local buffering and replay after WAN reconnection |

## Performance Targets

| Metric | Target |
|---|---:|
| End-to-end latency (p95) | under 2,000 ms |
| Query latency (p95) | under 500 ms |
| Inference throughput | 5-10 FPS per camera |
| System availability | 99.5% or better |
| Kafka consumer lag | under 10,000 messages |

## Hardware Guidance by Scenario

### Pilot

| Resource | Guidance |
|---|---|
| Cameras | 4 |
| CPU | 12+ threads |
| RAM | 16 GB minimum |
| Disk | 50 GB SSD |
| GPU | Not required |
| OS | Ubuntu 24.04 LTS |

### Small production

| Resource | Guidance |
|---|---|
| Cameras | Up to 10 |
| Core host CPU | 16+ threads |
| Core host RAM | 32 GB |
| Core host disk | 200 GB NVMe SSD |
| GPU | 1 NVIDIA GPU with 24 GB class VRAM recommended |
| Edge gateway | 4+ threads, 8 GB RAM, 50 GB SSD |

### Medium production

| Resource | Guidance |
|---|---|
| Cameras | Up to 50 |
| GPU nodes | 2 |
| Kafka brokers | 3 |
| TimescaleDB | 1 node, 64 GB RAM, 1 TB NVMe |
| MinIO | 1 node, 2 TB storage |
| Service nodes | 2 |
| Monitoring node | 1 |

### Large production

| Resource | Guidance |
|---|---|
| Cameras | 100+ |
| GPU nodes | 3+ as scene load increases |
| Storage | Distributed object storage recommended |
| Database | Primary plus read replica recommended |
| Core network | 10 GbE recommended |

## Accelerator Options

| Deployment area | Typical options |
|---|---|
| Central inference | NVIDIA data-center or workstation GPUs sized for deployment load |
| Edge inference | Jetson-compatible edge option available |
| Planning rule | Measured planning assumes approximately 32 camera-equivalents per GPU under normal operating conditions |

## Software Requirements

| Item | Guidance |
|---|---|
| Linux | Ubuntu 22.04 LTS or newer; current deployment documentation targets Ubuntu 24.04 LTS |
| Container runtime | Docker 24+ |
| Orchestration | Docker Compose for pilot, Ansible for production |
| Infrastructure automation | Terraform optional for cloud provisioning |
| Python | 3.11+ for setup and operational scripts |

## Network Planning

| Metric | Planning estimate |
|---|---|
| Camera stream bandwidth | 5 Mbps per camera |
| Edge-to-core bandwidth after filtering | approximately 250 Kbps per camera |
| Core network minimum | 1 GbE |
| Core network recommended for medium/large | 10 GbE |
| Camera network model | Camera VLAN to edge gateway, then secure transport to core |

## Storage Planning

The platform is designed to be compute-bound rather than storage-bound. Recent measured planning assumptions indicate:

| Storage class | Planning estimate |
|---|---|
| Hot frame storage | about 5 GB per camera per day at a typical 12% motion duty cycle |
| Warm clip and thumbnail storage | usually well under 0.1 GB per camera per day |
| Cold debug and archive storage | small relative to hot storage; planning figure is roughly 0.02 GB per camera per day |
| Retention guidance | 7-day hot frame retention, 30-90 day clip and archive windows, deployment-specific governance for metadata |

Actual storage growth depends on motion levels, clip frequency, retention policy, and site operating profile.

## Security and Administration

| Area | Current posture |
|---|---|
| Access control | JWT-based RBAC with admin, operator, viewer, and engineering roles |
| Data scoping | Per-camera scope filtering |
| Auditability | Audit logging on API requests |
| Edge-to-core security | mTLS |
| Core message bus security | Kafka SASL_SSL with SCRAM-SHA-256 |
| Multi-site isolation | Per-site PKI isolation |
| Recovery readiness | Documented backup, restore, and DR procedures with defined targets |

## API and Data Exchange

| Interface | Purpose |
|---|---|
| REST API | Search, retrieval, and operational workflows |
| Protobuf contracts | Internal service-to-service message schema |
| Kafka topics | Platform-native event and metadata transport |
| Export formats | Search results, evidence assets, operational reports |

## Commercial Note

Final sizing depends on camera density, motion profile, object mix, retention policy, and deployment topology. Contact sales or solution engineering for a deployment-specific bill of materials and rollout plan.
