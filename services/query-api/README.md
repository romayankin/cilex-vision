# Query API

Read-only REST API for querying detections, tracks, and events from the Cilex Vision platform.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | /detections | Paginated detection query (TimescaleDB) |
| GET | /tracks | Paginated track listing |
| GET | /tracks/{id} | Track detail with attributes |
| GET | /events | Paginated event listing with clip URLs |
| GET | /health | Health check |
| GET | /ready | Readiness check (DB connectivity) |
| GET | /metrics | Prometheus metrics |
| GET | /docs | OpenAPI documentation |

## Authentication

JWT via httpOnly cookie (`access_token`). Four RBAC roles:

| Role | Endpoints | Camera Scope |
|---|---|---|
| admin | all | all cameras |
| operator | detections, tracks, events | scoped |
| viewer | detections, tracks, events | scoped |
| engineering | detections, tracks | scoped |

## Configuration

Environment variables with `QUERY_` prefix override YAML config.

| Variable | Default | Description |
|---|---|---|
| `QUERY_DB__DSN` | `postgresql://cilex:cilex@localhost:5432/cilex` | asyncpg DSN |
| `QUERY_JWT__SECRET_KEY` | `change-me-in-production` | JWT signing key |
| `QUERY_MINIO__ENDPOINT` | `localhost:9000` | MinIO endpoint |
| `QUERY_MINIO__SIGNED_URL_EXPIRY_S` | `3600` | Signed URL TTL |

## Running

```bash
python main.py --config config.yaml
```

## Testing

```bash
cd services/query-api
python -m pytest tests/ -v
```
