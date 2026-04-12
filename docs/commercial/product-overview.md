# Cilex Vision Product Overview

Cilex Vision is a multi-camera video analytics platform for organizations that need faster visibility across one site or many. It turns existing camera feeds into searchable operational data: detections, tracked movement, events, clip evidence, and optional license plate results. The result is a system that helps teams review less video manually, respond faster, and manage larger estates with more consistency.

## Executive Summary

Many security and operations teams already have camera coverage, but they still depend on manual monitoring, fragmented tools, and time-consuming video review after an incident. Cilex Vision addresses that gap by converting camera footage into structured, searchable intelligence.

For business stakeholders, the value is straightforward:

- fewer hours spent reviewing video manually
- faster investigation turnaround
- better visibility across multiple cameras and sites
- stronger operational consistency for security, safety, and compliance workflows
- flexible deployment options that support on-premises, hybrid, and multi-site environments

The platform is designed for organizations that want real-time awareness without being locked into a single camera vendor or a single deployment model.

## What the Platform Delivers

### Detection and tracking

Cilex Vision recognizes and tracks seven core object classes:

- person
- car
- truck
- bus
- bicycle
- motorcycle
- animal

This gives operators a live, structured view of activity rather than raw video alone. Tracks remain searchable over time, which supports investigation, reporting, and operational follow-up.

### Cross-camera movement visibility

For sites with overlapping or connected camera coverage, the platform links the same subject across multiple cameras at the same site using visual similarity matching. This supports cross-camera journey analysis and reduces the need to manually “hop” between cameras during an investigation.

For larger estates, zone-aware sharding allows the same matching approach to scale to 50+ camera sites without losing the ability to handle boundary crossings between operational areas.

### Event-driven operations

The platform converts movement patterns into operational events, including:

- scene entry and exit
- stopped vehicle detection
- loitering
- motion started and motion ended

These events give operators a smaller, more actionable set of items to review instead of continuous video streams.

### Evidence and search

Operators can search detections, tracks, and events with filters such as camera, time window, object type, and state. The platform also supports clip generation and thumbnail creation for investigation workflows, making it easier to package evidence for review and escalation.

### License plate recognition

For vehicle-focused deployments, the platform includes an optional license plate recognition workflow with a plate-detection stage followed by OCR. Plate results can be searched through the Query API using exact, prefix, or wildcard matching.

### Multi-site oversight

For distributed estates, the platform includes a multi-site portal model with site health visibility, site creation workflows, cross-site comparison views, and per-site settings management. This supports regional or national operations teams that need a centralized view across multiple facilities.

## Deployment Models

### Single-site pilot

For proof-of-concept or early rollout, Cilex Vision supports a compact pilot footprint suitable for smaller camera counts and evaluation programs.

### Single-site production

For production sites, the platform scales to dedicated GPU-backed inference, operational search, event processing, and evidence handling for a larger number of cameras.

### Multi-site deployment

For larger organizations, Cilex Vision supports centralized management across multiple sites with per-site isolation, monitoring, and deployment automation.

### Edge + core architecture

The platform is designed for environments where network quality is variable. Edge components can continue buffering locally and replay data after reconnection, which helps maintain operational continuity when WAN links are unstable.

## Security and Privacy Highlights

Cilex Vision is designed for operational environments where access control, data handling, and deployment isolation matter.

Key controls include:

- role-based access control for admin, operator, viewer, and engineering users
- per-camera scope filtering so access can be limited to assigned camera groups
- audit logging on API access
- encrypted service-to-service transport between edge and core components
- per-site PKI isolation for multi-site environments
- documented backup, restore, and disaster recovery procedures with defined recovery targets

The current platform scope does not include face recognition or named-person identity lookup. That boundary is important for privacy posture and for customers who want analytics without expanding into biometric identification.

## Integration and Extensibility

Cilex Vision is designed to fit into existing operational environments rather than force a complete rip-and-replace.

Available integration points include:

- REST API access for search and operational workflows
- exportable clips and thumbnails for case review
- annotation workflow support through CVAT for model improvement programs
- infrastructure automation through Terraform and Ansible for repeatable rollout

For enterprise buyers, this means the platform can support both immediate operational needs and longer-term process integration.

## Performance and Scale Targets

The current platform targets:

| Metric | Target |
|---|---:|
| End-to-end latency (p95) | under 2,000 ms |
| Query latency (p95) | under 500 ms |
| Inference throughput | 5-10 FPS per camera |
| System availability | 99.5% or better |
| Deployment scale | up to 100 cameras per deployment |

These targets give stakeholders a practical benchmark for real-time operations, investigation responsiveness, and scale planning.

## Business Outcomes

Organizations typically look at Cilex Vision for one or more of the following outcomes:

- improve incident response time
- reduce manual review workload
- increase site coverage without scaling headcount linearly
- create a consistent operating model across many cameras or many sites
- make evidence easier to search, retrieve, and share internally

## Who It Is For

Cilex Vision fits organizations that need operational video analytics without a research-heavy deployment effort. Typical buyers and sponsors include:

- security leaders
- operations directors
- facilities and estate managers
- public safety and transport operators
- procurement and IT teams evaluating camera analytics platforms

## Commercial Note

Commercial packaging, deployment scope, and support terms should be finalized per customer environment. Final entitlements, sizing, and implementation plans are deployment-specific. Contact sales for solution design and tiering guidance.
