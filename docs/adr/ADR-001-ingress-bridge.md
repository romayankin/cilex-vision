---
status: STUB — to be completed by task P0-D06
---

# ADR-001: Ingress Bridge Service

> **⚠️ This is a placeholder.** The full ADR will be produced by a DESIGN agent executing task **P0-D06**.

## Context (draft)
Edge agents publish to local NATS JetStream. Central services consume from Kafka. A bridge is needed.

## Decision (draft)
Build a dedicated Ingress Bridge service.

## Key Responsibilities (draft)
1. Consume from NATS durably
2. Validate Protobuf schema
3. Offload large blobs to MinIO
4. Produce to Kafka with idempotent keys
5. Spool locally when Kafka unavailable
6. Rate-limit per site
