# LPR Service

License plate recognition service for vehicle tracklets.

## What It Does

1. Consumes `Tracklet` protobufs from `tracklets.local`
2. Filters to `car`, `truck`, and `bus`
3. Resolves a representative decoded frame from MinIO
4. Runs Triton plate detection on the vehicle crop
5. Applies a plate-specific quality gate
6. Runs Triton OCR on the detected plate crop
7. Buffers the best per-track result and writes it to `lpr_results`

## Notes

- LPR is feature-flagged via `enabled`; when disabled, the service exposes metrics and idles without consuming Kafka.
- The current upstream schemas do not provide a canonical frame URI on `tracklets.local`, so frame lookup uses the best detection row plus a MinIO object-time heuristic under `decoded-frames`.
- Query API search is exposed separately in `services/query-api`.

## Legal / Compliance

License-plate recognition is highly jurisdiction-specific. This repository does not ship a hardcoded country allow-list because deployability depends on local law, site purpose, signage, retention policy, and whether plates are linked to identified persons. Only enable this module after local legal review for the deployment jurisdiction.

## Running

```bash
python main.py --config config.yaml
```

## Testing

```bash
cd services/lpr-service
python -m pytest tests/ -q
```
