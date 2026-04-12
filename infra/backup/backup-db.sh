#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
TimescaleDB/PostgreSQL logical backup.

Usage:
  backup-db.sh [--output-dir /backups/postgres] [--container timescaledb]

Options:
  --output-dir PATH      Backup directory. Default: backups/postgres under repo root
  --container NAME       Database container name. Default: $CONTAINER_NAME or timescaledb
  --retention-days N     Remove DB backups older than N days. Default: 7
  -h, --help             Show this help

Environment:
  POSTGRES_USER          Default: cilex
  POSTGRES_DB            Default: vidanalytics
  BACKUP_DIR             Default output directory
  CONTAINER_NAME         Default container name
  RETENTION_DAYS         Default retention window in days
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

OUTPUT_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups/postgres}"
CONTAINER_NAME="${CONTAINER_NAME:-timescaledb}"
POSTGRES_USER="${POSTGRES_USER:-cilex}"
POSTGRES_DB="${POSTGRES_DB:-vidanalytics}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --container)
      CONTAINER_NAME="$2"
      shift 2
      ;;
    --retention-days)
      RETENTION_DAYS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || {
  printf 'RETENTION_DAYS must be a non-negative integer\n' >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required\n' >&2
  exit 1
}

docker inspect "$CONTAINER_NAME" >/dev/null 2>&1 || {
  printf 'Container not found: %s\n' "$CONTAINER_NAME" >&2
  exit 1
}

container_running="$(docker inspect --format='{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)"
[[ "$container_running" == "true" ]] || {
  printf 'Container is not running: %s\n' "$CONTAINER_NAME" >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"

timestamp="$(date -u +%Y%m%d-%H%M%S)"
dump_file="${OUTPUT_DIR}/${POSTGRES_DB}-${timestamp}.dump"
globals_file="${OUTPUT_DIR}/${POSTGRES_DB}-globals-${timestamp}.sql"
tmp_dump="$(mktemp "${OUTPUT_DIR}/.${POSTGRES_DB}-${timestamp}.XXXXXX.dump.part")"
tmp_globals="$(mktemp "${OUTPUT_DIR}/.${POSTGRES_DB}-globals-${timestamp}.XXXXXX.sql.part")"

cleanup() {
  rm -f "$tmp_dump" "$tmp_globals"
}
trap cleanup EXIT

printf 'Backing up %s from container %s...\n' "$POSTGRES_DB" "$CONTAINER_NAME"
docker exec "$CONTAINER_NAME" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc >"$tmp_dump"
docker exec "$CONTAINER_NAME" pg_dumpall -U "$POSTGRES_USER" --globals-only >"$tmp_globals"

[[ -s "$tmp_dump" ]] || {
  printf 'Backup failed: empty dump file\n' >&2
  exit 1
}
[[ -s "$tmp_globals" ]] || {
  printf 'Backup failed: empty globals file\n' >&2
  exit 1
}

mv "$tmp_dump" "$dump_file"
mv "$tmp_globals" "$globals_file"

deleted_count=0
while IFS= read -r stale_file; do
  [[ -n "$stale_file" ]] || continue
  rm -f "$stale_file"
  deleted_count=$((deleted_count + 1))
done < <(
  find "$OUTPUT_DIR" -maxdepth 1 -type f \
    \( -name "${POSTGRES_DB}-*.dump" -o -name "${POSTGRES_DB}-globals-*.sql" \) \
    -mtime "+${RETENTION_DAYS}" -print | sort
)

printf 'Backup complete.\n'
printf '  dump:    %s\n' "$dump_file"
printf '  globals: %s\n' "$globals_file"
printf '  pruned:  %d file(s)\n' "$deleted_count"

