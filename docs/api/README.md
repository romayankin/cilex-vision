# Query API Documentation

## Overview

The Query API provides read-oriented access to Cilex Vision metadata and operator tooling:

- detections from the TimescaleDB hypertable
- local tracks and joined track attributes
- events with signed clip URLs
- site topology read/write operations
- engineering debug traces from MinIO

Default base URL:

```text
http://localhost:8000
```

The canonical machine-readable contract lives in [openapi.yaml](openapi.yaml). A Postman collection generated from that spec is committed at [postman-collection.json](postman-collection.json).

## Authentication

The API expects a JWT in the httpOnly cookie named `access_token`.

- Cookie name: `access_token`
- Transport: cookie only, not `Authorization: Bearer`
- Token issuer: external to the Query API

The Query API does not expose a login endpoint. In production, the token is issued by the deployment's auth/admin plane. For local testing, operators usually mint a short-lived JWT with the same secret configured in `QUERY_JWT__SECRET_KEY`.

Example local token minting flow:

```bash
export QUERY_JWT__SECRET_KEY=change-me-in-production
python3 - <<'PY'
import time
import jwt

payload = {
    "sub": "00000000-0000-0000-0000-000000000001",
    "username": "operator-demo",
    "role": "operator",
    "camera_scope": ["cam-01", "cam-02"],
    "iat": int(time.time()),
    "exp": int(time.time()) + 3600,
}
print(jwt.encode(payload, "change-me-in-production", algorithm="HS256"))
PY
```

Then use the resulting token value with curl or Postman:

```bash
curl --cookie "access_token=$TOKEN" "http://localhost:8000/health"
```

### Role Matrix

| Role | Detections | Tracks | Events | Topology | Debug traces | Camera scope |
|---|---|---|---|---|---|---|
| `admin` | yes | yes | yes | read/write | yes | bypass |
| `operator` | yes | yes | yes | read | no | enforced on detections/tracks/events |
| `viewer` | yes | yes | yes | no | no | enforced on detections/tracks/events |
| `engineering` | yes | yes | no | no | yes | enforced on detections/tracks/events |

Current implementation note:

- `camera_scope` filtering is applied on `/detections`, `/tracks`, and `/events`.
- `/topology/*` and `/debug/*` are role-gated but not additionally filtered by `camera_scope`.

## Rate Limits

There is no rate limiting in the current implementation.

Operational note:

- treat the API as an internal control-plane service
- front it with a gateway or reverse proxy if per-user rate limiting becomes necessary

## Endpoint Summary

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/detections` | `admin`, `operator`, `viewer`, `engineering` | Paginated detection search with time, camera, class, and confidence filters |
| `GET` | `/tracks` | `admin`, `operator`, `viewer`, `engineering` | Paginated local track search |
| `GET` | `/tracks/{local_track_id}` | `admin`, `operator`, `viewer`, `engineering` | Single-track detail with joined attributes |
| `GET` | `/events` | `admin`, `operator`, `viewer` | Paginated event search with signed `clip_url` when available |
| `GET` | `/topology/{site_id}` | `admin`, `operator` | Full topology graph for a site |
| `PUT` | `/topology/{site_id}/edges` | `admin` | Create or update a topology edge |
| `POST` | `/topology/{site_id}/cameras` | `admin` | Add a camera to a site |
| `DELETE` | `/topology/{site_id}/cameras/{camera_id}` | `admin` | Remove a camera from a site |
| `GET` | `/debug/traces` | `engineering`, `admin` | List debug traces from the `debug-traces` bucket |
| `GET` | `/debug/traces/{trace_id}` | `engineering`, `admin` | Fetch a full trace JSON document |
| `GET` | `/health` | public | Liveness check |
| `GET` | `/ready` | public | Readiness check with DB connectivity probe |
| `GET` | `/metrics` | public | Prometheus metrics exposition |

## Pagination

List endpoints use `offset` / `limit` pagination.

- default `limit`: `50`
- maximum `limit`: `1000`
- `offset` starts at `0`

Response envelopes are endpoint-specific:

- `/detections` returns `detections`, `total`, `offset`, `limit`
- `/tracks` returns `tracks`, `total`, `offset`, `limit`
- `/events` returns `events`, `total`, `offset`, `limit`
- `/debug/traces` returns `traces`, `total`

## Error Responses

The common API error statuses are:

| Status | Meaning |
|---|---|
| `401` | Missing, expired, or invalid `access_token` cookie |
| `403` | Role is valid but not allowed for the endpoint |
| `404` | Requested resource does not exist or is not visible to the caller |
| `409` | Write conflict on topology camera creation |
| `422` | Validation error in path, query, or request body |
| `503` | Required dependency such as PostgreSQL or MinIO is unavailable |

Example error body:

```json
{
  "detail": "Authentication required"
}
```

## Signed URLs

The Query API signs MinIO download links before returning them to the client.

- `clip_url` in `/events` is signed for 1 hour when `clip_uri` exists and MinIO is configured
- `thumbnail_url` exists in the track-detail schema, but the current implementation returns `null` because thumbnail/frame-reference wiring is not in place yet
- debug trace listing returns signed URLs to trace JSON blobs in MinIO

Signed URL TTL is controlled by `QUERY_MINIO__SIGNED_URL_EXPIRY_S` and defaults to `3600` seconds.

## Common Query Patterns

Examples live in [examples/](examples/):

- [search-detections.sh](examples/search-detections.sh) searches detections by camera and time range
- [search-events.sh](examples/search-events.sh) filters events by type
- [get-journey.py](examples/get-journey.py) fetches the currently available track journey context

Current API limitation:

- there is no dedicated `global_track_links` or cross-camera journey endpoint yet
- the Python example combines track detail, related events, and optional topology context instead

## Postman Collection

Import [postman-collection.json](postman-collection.json) into Postman.

Collection variables:

- `base_url` — defaults to `http://localhost:8000`
- `access_token` — paste the JWT value only, without `access_token=`

To regenerate the artifacts:

```bash
python3 scripts/api/generate_openapi.py --output docs/api/openapi.yaml
python3 scripts/api/generate_postman_collection.py \
  --input docs/api/openapi.yaml \
  --output docs/api/postman-collection.json
```
