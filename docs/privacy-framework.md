---
version: "1.0.0"
status: P0-X02
created_by: scaffold
authored_by: codex-doc-agent
date: "2026-04-07"
---

# Privacy & Compliance Framework

This document is the engineering baseline for privacy and compliance controls in the Cilex Vision platform. It is not legal advice. It defines what the platform stores, who may access it, which controls are already implemented, and which hooks must exist before a production rollout.

## 1. Scope and Operating Assumptions

- The platform processes video-derived personal data from multi-camera analytics deployments.
- The current product scope is object detection, tracking, attributes, events, and engineering debug traces. It does not yet implement face recognition or identity lookup.
- Transport controls already defined in the architecture remain mandatory:
  - NATS edge-to-core links use mTLS with certificate identity and `verify_and_map`.
  - Kafka uses `SASL_SSL` with `SCRAM-SHA-256`.
  - User-facing object access uses signed MinIO GET URLs with short expiry.
- Camera scope is the default tenancy boundary. Only the `admin` role bypasses camera scope in the query API.

## 2. Data Classification

| Class | Actual Storage Location | Retention | Access | Encryption |
|-------|-------------------------|-----------|--------|------------|
| Raw video / raw frame captures | Today: MinIO `frame-blobs` for sampled frame objects written by the ingress bridge. Planned production buckets also include `raw-video` and `archive-warm` in Ansible inventory. | Baseline 30 days for footage-grade objects. `raw-video` 30-day lifecycle is defined in `infra/ansible/group_vars/all.yml`, but local compose does not currently create the `raw-video` bucket and the repo has no committed `infra/minio/lifecycle/` policy files. | `admin` direct access only. `operator` access only through approved incident export workflows. No routine `viewer` or `engineering` access. | In transit: MinIO access over TLS, signed URLs only for human retrieval. At rest: encrypted object storage / encrypted disks are required; bucket-level SSE/KMS enforcement is a deployment control that must be verified per site. |
| Event clips | MinIO `event-clips`; referenced by `events.clip_uri` and exposed through the query API as signed URLs. | 90 days. This lifecycle is defined in `infra/ansible/group_vars/all.yml`; the repo does not yet commit the rendered MinIO lifecycle policy artifacts. | `admin`, `operator`, and `viewer` on scoped cameras. `engineering` has no routine clip access. | In transit: HTTPS + signed URL distribution. At rest: encrypted object storage / encrypted disks required. |
| Metadata | TimescaleDB hypertables `detections` and `track_observations`; PostgreSQL relational tables `events`, `local_tracks`, `global_tracks`, `global_track_links`, `track_attributes`, `cameras`, and related lookup data. | `detections` and `track_observations`: 30 days, implemented via Timescale retention policy. Relational metadata: currently indefinite in schema; treat 365 days as the governance target and add explicit retention jobs before production if the customer contract requires a hard ceiling. | `admin`, `operator`, and `viewer` can read operational metadata on scoped cameras; `engineering` can read detections and tracks on scoped cameras for diagnostics. | In transit: DB connections must run over TLS on production networks. At rest: encrypted database volumes and encrypted backups are required. |
| Thumbnails | MinIO `thumbnails` bucket, currently provisioned in local compose but not yet exposed by a stable query endpoint. | No longer than the parent asset. Until thumbnail lineage is implemented, default to 30 days so thumbnails do not outlive stricter raw-frame retention. | `admin`, `operator`, and `viewer` on scoped cameras once exposed. `engineering` only when bundled into a debug or incident workflow. | In transit: HTTPS + signed URLs. At rest: encrypted object storage / encrypted disks required. |
| Embeddings | Kafka compacted topic `mtmc.active_embeddings`; optional references such as `track_observations.embedding_ref` may point to external storage later. | Active-state only in Kafka: latest value retained by compaction, tombstones kept for 24 hours. Any future object-store copy must not outlive the parent track without explicit approval. | Internal services only by default. No routine end-user access. `engineering` inspection is allowed only through controlled debug tooling. | In transit: Kafka `SASL_SSL` + `SCRAM-SHA-256`. At rest: encrypted broker volumes and encrypted backups required. |
| Debug traces | MinIO `debug-traces`; listed and retrieved through `GET /debug/traces` and `GET /debug/traces/{trace_id}`. | 30 days. This is implemented in the debug trace pipeline and intended to auto-expire at bucket level. | `engineering` and `admin` only. Engineering remains camera-scoped; `operator` and `viewer` are denied. | In transit: HTTPS + signed URLs for retrieval, internal MinIO API over TLS. At rest: encrypted object storage / encrypted disks required. |
| Audit logs | PostgreSQL `audit_logs`, populated by query API middleware on every API request except health/docs endpoints. | 2 years per the audit middleware contract. | `admin` only for routine read/export. No `viewer`, `operator`, or `engineering` access by default. | In transit: DB connections over TLS. At rest: encrypted database volumes and encrypted backups required. |

### Classification notes

- The highest-risk gap today is lifecycle completeness. Timescale retention is implemented for the two hypertables, and debug traces implement a 30-day bucket lifecycle, but the repository does not yet ship committed MinIO lifecycle artifacts for `raw-video`, `event-clips`, or `thumbnails`.
- The metadata class spans two storage behaviors:
  - High-volume, time-series metadata is short-lived and compressed.
  - Relational metadata is currently indefinite unless a deployment adds explicit retention jobs.
- Embeddings are not exposed as a user-facing feature, but they still deserve restricted handling because they support cross-camera linkage.

## 3. RBAC Model

### 3.1 Role semantics

- `admin`
  - Full administrative and privacy-governance role.
  - Bypasses camera scope in the current query API.
- `operator`
  - Operational user for incident review and day-to-day monitoring.
  - Scoped to the cameras listed in the JWT `camera_scope`.
- `viewer`
  - Read-only operational user.
  - Scoped to the cameras listed in the JWT `camera_scope`.
- `engineering`
  - Diagnostic role for system debugging and quality investigation.
  - Scoped to the cameras listed in the JWT `camera_scope`.

### 3.2 Permissions matrix

| Capability | admin | operator | viewer | engineering |
|------------|-------|----------|--------|-------------|
| Read detections | All cameras | Scoped read | Scoped read | Scoped read |
| Read tracks | All cameras | Scoped read | Scoped read | Scoped read |
| Read events | All cameras | Scoped read | Scoped read | No access |
| Retrieve event clip URLs | All cameras | Scoped read | Scoped read | No access |
| Retrieve thumbnail URLs | All cameras | Scoped read | Scoped read | No routine access |
| Retrieve raw video / archive exports | Approved direct access | Request-based access only | No access | No access |
| Retrieve debug traces | All cameras | No access | No access | Scoped read |
| Read audit logs | Full access | No access | No access | No access |
| Run DSAR export job | Yes | Request only, not direct execution | No access | No access |
| Run deletion job | Yes | No access | No access | No access |
| Change retention policy / override camera retention | Yes | No access | No access | No access |

### 3.3 Code alignment

- Current code-enforced baseline in `services/query-api/auth/jwt.py`:
  - `admin`: `detections`, `tracks`, `events`, `audit`
  - `operator`: `detections`, `tracks`, `events`
  - `viewer`: `detections`, `tracks`, `events`
  - `engineering`: `detections`, `tracks`
- Debug trace access is enforced separately at router level with `require_role("engineering", "admin")`.
- Privacy-sensitive workflows that do not yet exist in code, such as DSAR export, deletion, or retention override, must be implemented as admin-only endpoints or admin-approved jobs and must be audited.

## 4. Architectural Hooks Checklist

| Hook | Current State | Required Action |
|------|---------------|-----------------|
| Auto-delete per data class per retention policy | Partial. `detections` and `track_observations` have Timescale retention; debug traces implement a 30-day lifecycle; `raw-video` and `event-clips` lifecycle durations exist in Ansible vars. | Add idempotent MinIO lifecycle reconciliation for `raw-video`, `event-clips`, and `thumbnails`; document whether relational metadata remains indefinite or gains explicit retention jobs. |
| Audit trail on every data access | Partial. Query API middleware writes to `audit_logs` for API requests. | Extend auditing to privacy-sensitive export/deletion jobs and to object-store retrieval paths. Signed URL issuance is logged today only indirectly; actual object GETs should be covered by MinIO audit logs or an equivalent access-log sink. |
| API endpoint for data subject deletion | Missing. | Add an admin-only deletion workflow API, preferably job-based rather than synchronous delete. It must support dry-run, approval metadata, camera/time scoping, audit logging, and coordinated deletes across PostgreSQL, TimescaleDB, and MinIO. |
| Configurable face-blur/redaction | Missing, Phase 3 item. | Add export-time redaction first, not destructive source mutation. Support face blur and region blur on clips, thumbnails, and raw-video exports; log who requested the export and which policy was applied. |
| Export for DSAR | Missing. | Add an admin-only export job that packages metadata, event clips, thumbnails, and relevant audit records for a camera/time-bounded request. The export path must support third-party redaction before download. |
| Per-camera retention override | Missing. | Add validated per-camera overrides in camera configuration, with a safe cap at or below tenant policy unless explicitly approved. The reconciler must update Timescale retention jobs where applicable and MinIO lifecycle rules where object data is stored. |

### Implementation guidance

- Prefer asynchronous privacy jobs over direct delete endpoints. Privacy operations span multiple stores and need durable progress tracking.
- Treat signed URL generation as a privacy event. Log who requested the URL, for which object, and with what expiry.
- Do not give `engineering` broad access to event clips or audit logs. Debug traces are the intended diagnostic artifact.
- If a future feature links video analytics outputs to named individuals, separate that identity map from the detection pipeline and add a stricter access boundary.

## 5. DPIA Trigger Criteria

Run a DPIA-equivalent privacy review before go-live or before any material system change when any of the following is true. Use this rule even when the local jurisdiction does not formally use the term "DPIA".

| Trigger | Why it matters |
|---------|----------------|
| The deployment performs systematic monitoring of publicly accessible areas at meaningful scale. | This is the clearest regulator-recognized trigger for surveillance risk. |
| Cameras cover workplaces, residential entrances, schools, healthcare settings, places of worship, or any area where people have a heightened expectation of privacy. | These contexts increase both the sensitivity of the footage and the impact of misuse. |
| The system is used to monitor employees, contractors, students, residents, or other captive populations. | Power imbalance raises fairness and proportionality concerns. |
| The deployment links tracks across cameras, builds movement profiles, or correlates video with access control, POS, HR, visitor logs, or other identity-bearing systems. | Cross-system linkage moves the platform from simple observation into profiling. |
| The customer requests biometric identification, face recognition, watchlists, license-plate linkage to named persons, or any other special-category or criminal-offence workflow. | These uses materially increase legal and reputational risk and need explicit review. |
| Raw video, clips, or derived metadata will be shared outside the direct operating team, including customers, law enforcement, or offshore support teams. | Data transfer and onward disclosure increase abuse and jurisdictional risk. |
| The customer wants automated decisions or automated escalations based on analytics outputs. | Automation can significantly affect people even when detection accuracy is high. |
| A site requests retention beyond the baseline windows in this document, or requests a new archive tier. | Longer retention increases misuse exposure and changes proportionality. |
| A regulator complaint, customer privacy complaint, material incident, or security breach has occurred at this site or tenant. | Existing harm or scrutiny means the previous assessment is no longer sufficient. |

### Practical internal rule

- Mandatory privacy review:
  - Any trigger above is enough.
- Mandatory legal or customer-governance sign-off:
  - Any biometric identification use.
  - Any employee-monitoring deployment.
  - Any deployment covering schools, healthcare, or residential access points.
  - Any cross-border data export outside the customer’s declared operating region.

## 6. References

### Internal architecture sources

- `docs/security-design.md`
- `services/query-api/auth/jwt.py`
- `services/query-api/auth/audit.py`
- `docs/taxonomy.md`
- `docs/adr/ADR-003-database-schema.md`
- `.agents/handoff/P0-D04.md`
- `.agents/handoff/P0-D08.md`
- `.agents/handoff/P1-V05.md`
- `.agents/handoff/P1-V06.md`
- `.agents/handoff/P1-V07.md`

### External regulator guidance

- EDPB SME guide on DPIAs: systematic monitoring of publicly accessible areas on a large scale is a mandatory DPIA trigger for high-risk processing.
  - https://www.edpb.europa.eu/sme-data-protection-guide/faq-frequently-asked-questions/answer/what-data-protection-impact_en
- UK ICO guidance on surveillance systems: surveillance usually requires a DPIA because of inherent privacy risk, and retention must be justified by purpose and proportionality.
  - https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/cctv-and-video-surveillance/guidance-on-video-surveillance-including-cctv/how-can-we-comply-with-the-data-protection-principles-when-using-surveillance-systems/
- Singapore PDPC guidance for CCTV access requests and retention policy operations: organizations must be able to provide access to CCTV footage unless an exception applies, respond as soon as reasonably possible, and maintain retention/security policies.
  - https://www.pdpc.gov.sg/-/media/Files/PDPC/PDF-Files/Advisory-Guidelines/Advisory-Guidelines-for-Management-Corporations-17-May-2022.pdf
