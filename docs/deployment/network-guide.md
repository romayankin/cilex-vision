---
version: "1.0.0"
status: P3-X01
created_by: claude-code
date: "2026-04-10"
---

# Network Guide

This document covers network architecture, VLAN configuration, firewall rules, certificate enrollment, DNS, NTP, and bandwidth planning for Cilex Vision deployments.

## Network Zones

The platform uses five security zones with strict isolation between them. See `docs/security-design.md` for the full trust model.

```
                    +-----------------+
                    |   Internet      |
                    +--------+--------+
                             |
                    +--------+--------+
                    | Operator/Admin  |  SSH, HTTPS, Grafana
                    +--------+--------+
                             |
              +--------------+--------------+
              |                             |
   +----------+----------+     +-----------+-----------+
   |   Core Messaging    |     |    Core Storage       |
   | Kafka, Schema Reg   |     | TimescaleDB, MinIO    |
   +----------+----------+     +-----------+-----------+
              |                             |
   +----------+----------+     +-----------+-----------+
   | Edge Control Plane  |     |  Application Services |
   | Edge Agent, NATS    |     | Query API, Frontend   |
   +----------+----------+     +-----------------------+
              |
   +----------+----------+
   |    Camera VLAN      |
   | Cameras, NVRs       |
   +---------------------+
```

| Zone | Contains | Allowed Egress |
|------|----------|----------------|
| Camera VLAN | Cameras, NVRs | RTSP/ONVIF to edge agents only |
| Edge Control Plane | Edge agent, site-local NATS | step-ca, core bridge, monitoring |
| Core Messaging | Ingress bridge, Kafka brokers | Kafka, MinIO, Prometheus |
| Core Storage | MinIO, TimescaleDB | Internal service traffic only |
| Operator/Admin | Dashboards, config management | Controlled SSH/HTTPS |

## Camera VLAN Setup

Cameras must be on a dedicated VLAN isolated from corporate networks and the internet.

### Requirements

- Dedicated VLAN ID for cameras (e.g., VLAN 100)
- No internet egress from camera VLAN
- No east-west camera-to-camera traffic (unless vendor-required)
- Only edge agent hosts can reach cameras on RTSP/ONVIF ports
- PoE on access ports (cameras are typically PoE-powered)

### Example Switch Configuration (Generic)

```
! Create camera VLAN
vlan 100
  name CAMERAS
  exit

! Configure camera access port
interface GigabitEthernet0/1
  description Camera-Entrance-01
  switchport mode access
  switchport access vlan 100
  spanning-tree portfast
  power inline auto
  exit

! Configure trunk to edge gateway
interface GigabitEthernet0/48
  description Edge-Gateway-Uplink
  switchport mode trunk
  switchport trunk allowed vlan 100,200
  exit

! ACL: block camera internet access
ip access-list extended CAMERA-VLAN-OUT
  permit tcp any host 10.42.10.11 eq 554       ! RTSP to edge gateway
  permit udp any host 10.42.10.11 eq 554       ! RTSP UDP
  permit tcp any host 10.42.10.11 range 80 443 ! ONVIF
  deny   ip any any
  exit

interface vlan 100
  ip access-group CAMERA-VLAN-OUT out
  exit
```

Adapt this to your switch vendor (Cisco, Juniper, Aruba, etc.). The key principle is: cameras can only reach edge agent hosts on RTSP (554) and ONVIF (80/443) ports.

## Firewall Rules

### Edge Zone Rules

| Source | Destination | Port | Protocol | Purpose |
|--------|-------------|------|----------|---------|
| Camera VLAN | Edge gateway | 554 | TCP/UDP | RTSP video streams |
| Camera VLAN | Edge gateway | 80, 443 | TCP | ONVIF discovery/control |
| Edge gateway | NATS (local) | 4222 | TCP | NATS client (mTLS) |
| Edge gateway | MinIO | 9000 | TCP | Frame blob upload |
| Edge gateway | step-ca | 443 | TCP | Certificate enrollment/renewal |
| Edge gateway | Prometheus | 9090 | TCP | Metrics scrape (node-exporter) |
| NATS (local) | Kafka brokers | 9093 | TCP | Ingress bridge forward |

### Core Zone Rules

| Source | Destination | Port | Protocol | Purpose |
|--------|-------------|------|----------|---------|
| Kafka brokers | Kafka brokers | 9092 | TCP | Inter-broker replication |
| Kafka brokers | Kafka brokers | 9094 | TCP | KRaft controller |
| Service nodes | Kafka brokers | 9093 | TCP | Kafka client (SASL_SSL) |
| Service nodes | TimescaleDB | 5432 | TCP | Database connections |
| Service nodes | MinIO | 9000 | TCP | Object storage API |
| Service nodes | Triton GPU | 8001 | TCP | gRPC inference |
| Service nodes | Triton GPU | 8000 | TCP | Triton HTTP API |
| Service nodes | Redis | 6379 | TCP | Cache, rate limiting |
| Monitoring | All nodes | 9090 | TCP | Prometheus metrics scrape |
| Monitoring | All nodes | 9100 | TCP | Node exporter scrape |

### Operator Zone Rules

| Source | Destination | Port | Protocol | Purpose |
|--------|-------------|------|----------|---------|
| Operator | All nodes | 22 | TCP | SSH administration |
| Operator | Grafana | 3000 | TCP | Dashboard access |
| Operator | Prometheus | 9090 | TCP | Metrics UI |
| Operator | Query API | 8000 | TCP | REST API / Swagger |
| Operator | MinIO console | 9001 | TCP | Storage management |
| Operator | MLflow | 5000 | TCP | Experiment tracking |
| Operator | CVAT | 8080 | TCP | Annotation tool |
| Operator | Frontend | 3000 | TCP | Web UI |

### Service Port Reference

| Service | Port | Protocol |
|---------|------|----------|
| Kafka internal (inter-broker) | 9092 | TCP |
| Kafka client (SASL_SSL) | 9093 | TCP |
| Kafka controller (KRaft) | 9094 | TCP |
| NATS client | 4222 | TCP |
| NATS HTTP monitoring | 8222 | TCP |
| TimescaleDB / PostgreSQL | 5432 | TCP |
| MinIO API | 9000 | TCP |
| MinIO console | 9001 | TCP |
| Triton HTTP | 8000 | TCP |
| Triton gRPC | 8001 | TCP |
| Triton metrics | 8002 | TCP |
| Prometheus | 9090 | TCP |
| Grafana | 3000 | TCP |
| MLflow | 5000 | TCP |
| CVAT | 8080 | TCP |
| Query API | 8000 | TCP |
| Redis | 6379 | TCP |
| Node Exporter | 9100 | TCP |
| step-ca | 443 | TCP |

## mTLS Certificate Flow

Edge-to-core communication uses mutual TLS with certificates issued by an internal step-ca PKI.

### Certificate Enrollment

```
1. Operator runs: bootstrap-site.sh --site-id site-01
   |
   v
2. step-ca issues certificates:
   - NATS server cert: CN=nats-site-01.edge.cilex.internal (90-day lifetime)
   - Edge client cert: CN=edge-pub.site-01.cilex.internal (90-day lifetime)
   - Ingress bridge cert: CN=bridge-sub.site-01.cilex.internal (90-day lifetime)
   |
   v
3. Certificates distributed to hosts:
   - NATS server: /etc/nats/certs/{server.crt, server.key, root_ca.crt}
   - Edge agent: /etc/cilex/certs/{client.crt, client.key, root_ca.crt}
   - Ingress bridge: /etc/cilex/certs/{bridge.crt, bridge.key, root_ca.crt}
   |
   v
4. NATS verify_and_map maps CN to subject-level permissions:
   - edge-pub.site-01 -> publish to frames.site-01.>
   - bridge-sub.site-01 -> subscribe to frames.site-01.>
   |
   v
5. Auto-renewal via internal-acme provisioner (daily check, renews at 2/3 lifetime)
```

### Certificate Rotation

Certificates auto-renew before expiry. For manual rotation:

```bash
# Reissue certificates for a site
bash infra/pki/bootstrap-site.sh --site-id site-01 --renew

# Verify certificate expiry
openssl x509 -in /etc/nats/certs/server.crt -noout -enddate
```

### Troubleshooting mTLS

```bash
# Test TLS handshake to NATS
openssl s_client -connect nats-site-01:4222 \
    -cert /etc/cilex/certs/client.crt \
    -key /etc/cilex/certs/client.key \
    -CAfile /etc/cilex/certs/root_ca.crt

# Check certificate chain
openssl verify -CAfile /etc/cilex/certs/root_ca.crt /etc/cilex/certs/client.crt
```

## DNS Requirements

### Internal Hostnames

All nodes must be resolvable by their inventory hostnames. Options:

1. **Internal DNS server** (recommended): Configure A records for all hosts
2. **`/etc/hosts`**: Acceptable for small deployments

Required DNS entries:

```
# Core
10.43.10.11  kafka-1.core.cilex.internal
10.43.10.12  kafka-2.core.cilex.internal
10.43.10.13  kafka-3.core.cilex.internal
10.43.20.11  timescaledb-1.core.cilex.internal
10.43.20.12  minio-1.core.cilex.internal
10.43.30.11  triton-1.core.cilex.internal
10.43.30.12  triton-2.core.cilex.internal
10.43.40.11  monitoring-1.core.cilex.internal
10.43.50.11  mlflow-1.core.cilex.internal
10.43.70.11  app-1.core.cilex.internal
10.43.70.12  app-2.core.cilex.internal

# Edge (per site)
10.42.10.11  edge-site-a.edge.cilex.internal
10.42.20.11  nats-site-a.edge.cilex.internal
```

### Certificate SANs

NATS server certificates include DNS SANs matching these hostnames. If you change the naming scheme, update `infra/pki/bootstrap-site.sh` to match.

## NTP Requirements

Accurate timestamps are critical for cross-camera event correlation. All nodes must sync to a common time source.

### Chrony Configuration

Install and configure Chrony on all nodes:

```bash
sudo apt install chrony
```

Edit `/etc/chrony/chrony.conf`:

```conf
# Use 3 NTP pools for redundancy
pool 0.pool.ntp.org iburst maxsources 4
pool 1.pool.ntp.org iburst maxsources 4
pool 2.pool.ntp.org iburst maxsources 4

# Allow serving time to local network (on the NTP server)
# allow 10.42.0.0/16
# allow 10.43.0.0/16

# Log measurements and statistics
log measurements statistics tracking

# Maximum clock drift threshold
makestep 1.0 3
```

Verify synchronization:

```bash
# Check sync status
chronyc tracking

# Check sources
chronyc sources -v

# Check drift
chronyc sourcestats
```

### Clock Drift Monitoring

The platform monitors clock drift via Prometheus. Alert thresholds (from `docs/time-sync-policy.md`):

| Level | Threshold | Action |
|-------|-----------|--------|
| OK | < 500 ms | Normal operation |
| WARN | 500 ms - 2000 ms | Investigate NTP sync |
| CRITICAL | > 2000 ms | Cross-camera correlation unreliable |

### Camera NTP

Cameras often have their own NTP settings. Configure cameras to use the same NTP source as the edge gateway. However, note that camera timestamps (`source_capture_ts`) are treated as **advisory and untrusted** -- the authoritative timestamp is `edge_receive_ts` set by the Chrony-synced edge host.

## Bandwidth Planning

### Per-Camera Estimates

| Stream | Bandwidth |
|--------|-----------|
| 1080p H.264 @ 25 FPS (main) | 4-8 Mbps |
| 720p H.264 @ 15 FPS (sub) | 1-3 Mbps |
| Planning estimate | 5 Mbps per camera |

### Edge-to-Core (After Filtering)

The edge agent filters ~85% of frames (motion detection). The ingress bridge forwards only sampled frame references:

| Data Type | Per-Camera |
|-----------|-----------|
| Frame blob upload (JPEG, 15% pass-through) | ~200 Kbps |
| Kafka metadata messages | ~10 Kbps |
| **Total per camera** | **~250 Kbps** |

### Aggregate by Scenario

| Scenario | Cameras | Camera VLAN Total | Edge-to-Core WAN |
|----------|---------|-------------------|------------------|
| Pilot | 4 | 20 Mbps | N/A (single host) |
| Small | 10 | 50 Mbps | 2.5 Mbps |
| Medium | 50 | 250 Mbps | 12.5 Mbps |
| Large | 100 | 500 Mbps | 25 Mbps |

### Core Internal Traffic

| Traffic Type | Bandwidth Estimate |
|-------------|-------------------|
| Kafka replication (3x) | 3x ingress rate |
| MinIO frame storage | = edge-to-core rate |
| TimescaleDB COPY ingest | ~1 Mbps per 10 cameras |
| Prometheus scrapes | < 1 Mbps total |
| Triton gRPC inference | ~5 Mbps per GPU node |

**Recommendation:** 1 Gbps minimum for all core nodes. 10 Gbps for medium/large deployments between Kafka, TimescaleDB, and MinIO nodes.
