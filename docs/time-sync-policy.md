---
status: STUB — to be completed by task P0-D07
---

# Time Synchronization & Timestamp Policy

> **⚠️ This is a placeholder.** The full policy will be produced by a DESIGN agent executing task **P0-D07**.

## Three-Timestamp Model (draft)

Every message carries:
1. **source_capture_ts** — camera/NVR reported time (advisory, untrusted)
2. **edge_receive_ts** — edge agent's NTP-corrected clock (PRIMARY for correlation)
3. **core_ingest_ts** — central ingress time (for lag/replay analysis)

## Drift Thresholds (draft)

- WARN: edge-to-edge skew >500ms at same site
- CRITICAL: edge-to-edge skew >2s
- MTMC uses edge_receive_ts as primary time coordinate
