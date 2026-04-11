# Operations Handbook

**Audience:** Platform operators responsible for day-to-day monitoring, maintenance, and incident response of the Cilex Vision multi-camera video analytics platform.

**Scope:** This handbook covers daily operational checks, monitoring and alerting, scheduled maintenance, and capacity planning. It does not cover application development or deployment procedures.

---

## Sections

| Document | Purpose |
|----------|---------|
| [Daily Checks](daily-checks.md) | Morning checklist for verifying platform health |
| [Monitoring Guide](monitoring-guide.md) | Dashboard-by-dashboard reference and alert response guide |
| [Maintenance Windows](maintenance-windows.md) | Scheduled maintenance procedures and checklists |
| [Capacity Planning](capacity-planning.md) | Resource inventory, scaling indicators, and growth projections |
| [Glossary](glossary.md) | Domain terminology, pipeline stages, and infrastructure terms |

---

## Quick Reference

### Key URLs

| Service | URL | Notes |
|---------|-----|-------|
| Grafana | `http://<grafana-host>:3000` | Dashboards and alerts |
| Prometheus | `http://<prometheus-host>:9090` | Metrics and alert rules |
| MinIO Console | `http://<minio-host>:9001` | Object storage management |
| Query API | `http://<query-api-host>:8080` | Detection/track/event search |
| Query API Health | `http://<query-api-host>:8080/health` | Liveness check |
| Triton Health | `http://<triton-host>:8000/v2/health/ready` | Inference server readiness |
| NATS Monitoring | `http://<nats-host>:8222/healthz` | Edge broker health |
| Kafka UI | `http://<kafka-host>:8080` | Topic and consumer group browser |

### Grafana Dashboards

| Dashboard | UID | Direct link |
|-----------|-----|-------------|
| Stream Health | `stream-health` | `/d/stream-health` |
| Inference Performance | `inference-performance` | `/d/inference-performance` |
| Bus Health | `bus-health` | `/d/bus-health` |
| Storage | `storage` | `/d/storage` |
| Model Quality | `model-quality` | `/d/model-quality` |
| MTMC Re-ID Health | `mtmc-health` | `/d/mtmc-health` |
| Shadow vs Production | `shadow-comparison` | `/d/shadow-comparison` |
| Storage Tiering | `storage-tiering` | `/d/storage-tiering` |

### Emergency Contacts

| Role | Contact | Availability |
|------|---------|-------------|
| Platform Engineering | _TBD_ | Business hours |
| Site Operations | _TBD_ | 24/7 |
| Database Engineering | _TBD_ | Business hours |
| Security / PKI | _TBD_ | Business hours |

---

## Related Documents

- [Incident Response Runbook](../runbooks/incident-response.md)
- [Service Restart Runbook](../runbooks/service-restart.md)
- [Scaling Runbook](../runbooks/scaling.md)
- [Backup and Restore Runbook](../runbooks/backup-restore.md)
- [Camera Onboarding Runbook](../runbooks/camera-onboarding.md)
- [Model Rollout SOP](../runbooks/model-rollout-sop.md)
