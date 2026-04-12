# Cilex Vision FAQ

## Privacy and Compliance

### Does Cilex Vision perform facial recognition?

No. The current platform scope is object detection, tracking, attributes, event generation, cross-camera linking, and optional license plate recognition. It does not provide face recognition or named-person identity lookup.

### How is data retained?

Retention is policy-driven. Current operational baselines are:

- frame storage in hot tiers for short operational windows
- event clips retained for longer review periods
- debug traces retained for engineering investigation windows
- relational metadata governed separately according to deployment policy

Retention should always be agreed as part of deployment scope and customer governance.

### Is the platform suitable for privacy-sensitive environments?

It can be deployed with strong controls, including role-based access, per-camera scope filtering, audit logging, encrypted service-to-service transport, and signed access to evidence assets. Final compliance assessment depends on customer use case, jurisdiction, and policy.

### Is Cilex Vision GDPR-ready?

The platform includes key technical controls needed for regulated environments, but legal and policy readiness depends on the customer deployment and governance model. Customers should treat the platform as a configurable technical foundation rather than a substitute for legal review.

## Integration

### Can Cilex Vision work with existing IP cameras?

Yes. The platform is designed around RTSP video ingest and ONVIF-based discovery and capability checks, which supports a broad range of vendor-neutral IP camera deployments.

### Can it integrate with existing operational systems?

Yes. The platform offers REST API access for search and retrieval workflows. Integration scope should be defined during solution design based on the target systems and operating model.

### Does it support export of clips and evidence?

Yes. The platform can generate investigation clips and thumbnails that support review, escalation, and evidence handling workflows.

### Can it support annotation and continuous model improvement?

Yes. The broader platform includes CVAT-based annotation workflows and continuous feedback tooling for operational model improvement programs.

## Scaling and Deployment

### How many cameras can the platform support?

Commercial guidance today is:

- Basic: up to 10 cameras
- Pro: up to 50 cameras
- Enterprise: 100+ cameras

Final sizing depends on motion levels, event density, retention policy, and deployment topology.

### Can it be deployed across multiple sites?

Yes. Multi-site deployment, centralized monitoring, and per-site isolation are part of the current platform scope.

### Does the platform support edge computing?

Yes. The platform supports edge processing and local buffering so sites can continue operating during temporary WAN disruption and replay data when connectivity returns.

### Can data stay on-premises?

Yes. The platform supports on-premises, hybrid edge-to-core, and centrally managed multi-site deployments. Final architecture depends on customer policy and infrastructure preference.

## Security

### How is access controlled?

Access is role-based with separate admin, operator, viewer, and engineering roles. Camera scope filtering can limit users to specific cameras or groups of cameras.

### How is traffic protected between edge and core?

The platform uses encrypted transport and per-site certificate isolation for edge-to-core communication, plus secured internal messaging for core services.

### Is there audit logging?

Yes. API requests are audited so operational access can be reviewed and governed.

## Operations

### What uptime level is the platform designed for?

The current availability target is 99.5% or better, subject to deployment design, infrastructure quality, and support model.

### How is the platform monitored?

The platform includes Prometheus and Grafana monitoring with multiple dashboards covering system health, inference performance, storage, MTMC health, shadow comparisons, and related operational views.

### What happens if a WAN link drops?

Edge components can buffer locally and replay after reconnection. This helps preserve continuity for remote or bandwidth-constrained sites.

### Is backup and restore included?

Yes. The platform includes documented disaster recovery procedures and operational automation for backup, restore, and recovery testing.

### What support options are available?

Support is packaged by commercial tier, from email support to priority support and dedicated escalation paths. Contact sales for the support model aligned to your deployment.
