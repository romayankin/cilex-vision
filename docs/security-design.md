---
status: STUB — to be completed by task P0-D08
---

# Security Design

> **⚠️ This is a placeholder.** The full security design will be produced by a DESIGN agent executing task **P0-D08**.

## Trust Model (draft)

- Cameras: UNTRUSTED (isolated VLAN, no internet access)
- Edge agents → NATS: mTLS with per-site client certificates
- Ingress bridge → Kafka: TLS + SASL_SSL (SCRAM-SHA-256)
- Internal services: service-account tokens or mTLS
- Object storage access: signed URLs with 1-hour expiry

## PKI (draft)

- Internal CA: step-ca (Smallstep)
- Per-site certificate issuance
- 90-day rotation cycle
