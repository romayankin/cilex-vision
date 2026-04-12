# Deployment Architecture

This document describes the three deployment topologies currently documented in the repository:

1. single-node pilot
2. multi-node production
3. multi-site

For sizing details, see [docs/deployment/hardware-requirements.md](../deployment/hardware-requirements.md).

## 1. Single-Node Pilot

The pilot is a compact Docker Compose deployment intended for evaluation, proof-of-concept work, and local validation.

```mermaid
flowchart LR
    cameras["Camera Network"]

    subgraph pilot["Single Host: Docker Compose Pilot"]
        edge["edge-agent"]
        nats["NATS"]
        bridge["ingress-bridge"]
        kafka["Kafka"]
        decode["decode-service"]
        inference["inference-worker"]
        attr["attribute-service"]
        events["event-engine"]
        clips["clip-service"]
        mtmc["mtmc-service"]
        lpr["lpr-service"]
        bulk["bulk-collector"]
        query["query-api"]
        minio["MinIO"]
        tsdb["TimescaleDB"]
        triton["Triton CPU or lightweight inference"]
        monitor["Prometheus + Grafana + Loki"]
        ui["frontend / browser access"]
    end

    cameras --> edge
    edge --> nats
    nats --> bridge
    bridge --> kafka
    kafka --> decode
    decode --> inference
    inference --> attr
    inference --> events
    inference --> mtmc
    inference --> lpr
    inference --> bulk
    query --> tsdb
    query --> minio
    ui --> query
    monitor --> pilot
```

### Characteristics

| Property | Pilot profile |
|---|---|
| Camera count | 4 |
| GPU | not required |
| Orchestration | Docker Compose |
| Typical use | evaluation, development, proof-of-concept |

## 2. Multi-Node Production

The production topology separates infrastructure roles onto dedicated hosts and is deployed through Ansible, optionally provisioned through Terraform.

```mermaid
flowchart LR
    subgraph edge["Edge / Site Hosts"]
        cameras["Camera VLAN"]
        edge_host["edge-agent + NATS + local buffer"]
    end

    subgraph core["Core Production Cluster"]
        kafka1["Kafka broker 1"]
        kafka2["Kafka broker 2"]
        kafka3["Kafka broker 3"]
        tsdb["TimescaleDB / PostgreSQL"]
        minio["MinIO"]
        gpu1["GPU node 1<br/>Triton"]
        gpu2["GPU node 2<br/>Triton"]
        svc1["Service node 1<br/>bridge, decode, inference, attribute, event, clip, mtmc, bulk, query, lpr"]
        svc2["Service node 2<br/>service failover / scale-out"]
        mon["Monitoring node<br/>Prometheus, Grafana, Loki"]
        mlflow["MLflow"]
    end

    cameras --> edge_host
    edge_host --> svc1
    edge_host --> svc2
    svc1 --> kafka1
    svc1 --> kafka2
    svc1 --> kafka3
    svc2 --> kafka1
    svc2 --> kafka2
    svc2 --> kafka3
    svc1 --> tsdb
    svc2 --> tsdb
    svc1 --> minio
    svc2 --> minio
    svc1 --> gpu1
    svc2 --> gpu2
    mon --> core
    mlflow --> core
```

### Characteristics

| Property | Multi-node production profile |
|---|---|
| Camera count | 10 to 100+ depending on sizing |
| GPU | dedicated inference hosts |
| Orchestration | Ansible |
| Provisioning | Terraform optional |
| Strength | role separation, operational isolation, better scale and fault containment |

## 3. Multi-Site

The multi-site topology adds one central core plus per-site edge stacks. Site automation is handled by Terraform and Ansible, with per-site PKI isolation.

```mermaid
flowchart LR
    subgraph siteA["Site Alpha"]
        camA["Cameras"]
        edgeA["edge-agent"]
        natsA["site-local NATS"]
        minioA["local MinIO buffer"]
    end

    subgraph siteB["Site Beta"]
        camB["Cameras"]
        edgeB["edge-agent"]
        natsB["site-local NATS"]
        minioB["local MinIO buffer"]
    end

    subgraph central["Central Core"]
        kafka["Kafka cluster"]
        tsdb["TimescaleDB / PostgreSQL"]
        minio["central MinIO"]
        triton["GPU pool / Triton"]
        services["Core services"]
        monitoring["Monitoring stack"]
        portal["Query API + frontend portal"]
    end

    camA --> edgeA
    edgeA --> natsA
    edgeA --> minioA
    natsA --> services

    camB --> edgeB
    edgeB --> natsB
    edgeB --> minioB
    natsB --> services

    services --> kafka
    services --> tsdb
    services --> minio
    services --> triton
    portal --> tsdb
    portal --> minio
    monitoring --> central
```

### Characteristics

| Property | Multi-site profile |
|---|---|
| Site isolation | per-site PKI and edge control plane isolation |
| Centralized functions | Kafka, database, object storage, inference, API, monitoring |
| Site-local functions | edge ingest, local NATS, local buffering, local object spool |
| Provisioning pattern | Terraform `central` + `site` modules |
| Operations pattern | site add/remove playbooks plus onboarding automation |

## Network Zones

```mermaid
flowchart LR
    cam["Camera VLAN<br/>Untrusted"]
    edge["Edge Control Plane<br/>edge-agent, NATS, local buffer"]
    core["Core Services<br/>Kafka, MinIO, DB, Triton, services"]
    api["API / UI Zone<br/>query-api, frontend"]
    ops["Ops / Admin Zone<br/>Ansible, Terraform, PKI, dashboards"]

    cam -->|RTSP / ONVIF only| edge
    edge -->|secure edge-to-core transport| core
    api -->|JWT-protected access| core
    ops -->|controlled admin access| edge
    ops -->|controlled admin access| core
```

## Hardware Guidance Cross-Reference

The architecture shown here aligns to the current deployment guidance:

| Scenario | Primary reference |
|---|---|
| Pilot | [hardware-requirements.md](../deployment/hardware-requirements.md#pilot-4-cameras) |
| Small / single-site production | [hardware-requirements.md](../deployment/hardware-requirements.md#small-10-cameras) |
| Medium / multi-node | [hardware-requirements.md](../deployment/hardware-requirements.md#medium-50-cameras) |
| Large / multi-site | [hardware-requirements.md](../deployment/hardware-requirements.md#large-100-cameras) |

## Current Implementation Notes

- The production and multi-site automation are additive to the pilot path; they do not replace the pilot Compose deployment.
- The multi-site portal exists in the frontend, but some site-management and comparison data still depend on backend APIs and real metrics that are not fully wired.
- The repo includes Jetson edge support, but Jetson should be treated as a deployment variant of `edge-agent`, not a separate logical architecture layer.
