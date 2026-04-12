#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: health-check-all.sh [--inventory infra/ansible/inventory/production.yml] [--local] [--no-color]

Options:
  --local              Use localhost defaults for docker-compose stack
                       (auto-detected when no other config is provided)

Environment overrides:
  KAFKA_HOSTS          Comma-separated broker hosts
  KAFKA_PORT           Default: 9093
  NATS_HOSTS           Comma-separated NATS hosts
  NATS_HTTP_PORT       Default: 8222
  TIMESCALEDB_HOSTS    Comma-separated database hosts
  TIMESCALEDB_PORT     Default: 5432
  MINIO_HOSTS          Comma-separated MinIO hosts
  MINIO_PORT           Default: 9000
  TRITON_HOSTS         Comma-separated Triton hosts
  TRITON_HTTP_PORT     Default: 8000
  PROMETHEUS_HOSTS     Comma-separated Prometheus hosts
  PROMETHEUS_PORT      Default: 9090
  GRAFANA_HOSTS        Comma-separated Grafana hosts
  GRAFANA_PORT         Default: 3000
  EDGE_AGENT_HOSTS     Comma-separated edge gateway hosts
  EDGE_AGENT_PORT      Default: 9090
  APP_ENDPOINTS        Semicolon-separated list of name=url or bare URLs
EOF
}

NO_COLOR=0
LOCAL_MODE=0
INVENTORY_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory)
      INVENTORY_PATH="$2"
      shift 2
      ;;
    --local)
      LOCAL_MODE=1
      shift
      ;;
    --no-color)
      NO_COLOR=1
      shift
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

if [[ -t 1 && "$NO_COLOR" -eq 0 && -z "${CI:-}" ]]; then
  COLOR_RED=$'\033[31m'
  COLOR_GREEN=$'\033[32m'
  COLOR_YELLOW=$'\033[33m'
  COLOR_RESET=$'\033[0m'
else
  COLOR_RED=""
  COLOR_GREEN=""
  COLOR_YELLOW=""
  COLOR_RESET=""
fi

declare -a CHECK_NAMES=()
declare -a CHECK_KINDS=()
declare -a CHECK_TARGETS=()
declare -a CHECK_RESULTS=()

append_check() {
  local kind="$1"
  local name="$2"
  local target="$3"
  CHECK_KINDS+=("$kind")
  CHECK_NAMES+=("$name")
  CHECK_TARGETS+=("$target")
}

append_csv_tcp_checks() {
  local label="$1"
  local csv_hosts="$2"
  local port="$3"
  local index=1
  local host
  IFS=',' read -r -a _hosts <<< "$csv_hosts"
  for host in "${_hosts[@]}"; do
    [[ -z "$host" ]] && continue
    append_check "tcp" "${label} ${index}" "${host}:${port}"
    index=$((index + 1))
  done
}

append_csv_http_checks() {
  local label="$1"
  local csv_hosts="$2"
  local port="$3"
  local path="$4"
  local index=1
  local host
  IFS=',' read -r -a _hosts <<< "$csv_hosts"
  for host in "${_hosts[@]}"; do
    [[ -z "$host" ]] && continue
    append_check "http" "${label} ${index}" "http://${host}:${port}${path}"
    index=$((index + 1))
  done
}

append_app_endpoints() {
  local raw="${1:-}"
  local entry name url
  [[ -z "$raw" ]] && return 0
  IFS=';' read -r -a _entries <<< "$raw"
  for entry in "${_entries[@]}"; do
    [[ -z "$entry" ]] && continue
    if [[ "$entry" == *=* ]]; then
      name="${entry%%=*}"
      url="${entry#*=}"
    else
      name="app"
      url="$entry"
    fi
    append_check "http" "$name" "$url"
  done
}

load_inventory_checks() {
  local inventory="$1"
  python3 - "$inventory" <<'PY'
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


inventory_path = Path(sys.argv[1]).resolve()
inventory = load_yaml(inventory_path)
group_vars = load_yaml(inventory_path.parent.parent / "group_vars" / "all.yml")

all_children = inventory.get("all", {}).get("children", {})


def host_group(name: str) -> dict:
    return all_children.get(name, {}).get("hosts", {}) or {}


def host_addr(hostname: str, hostvars: dict) -> str:
    return str((hostvars or {}).get("ansible_host", hostname))


def externalize_local_url(raw_url: str, host: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return raw_url
    netloc = host
    if parsed.port is not None:
        netloc = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


checks: list[tuple[str, str, str]] = []

kafka_port = int(group_vars.get("kafka_client_port", 9093))
for name, hostvars in host_group("kafka").items():
    checks.append(("tcp", f"Kafka {name}", f"{host_addr(name, hostvars)}:{kafka_port}"))

nats_http_port = int(group_vars.get("nats_http_port", 8222))
for name, hostvars in host_group("nats").items():
    checks.append(("http", f"NATS {name}", f"http://{host_addr(name, hostvars)}:{nats_http_port}/healthz"))

timescaledb_port = int(group_vars.get("timescaledb_port", 5432))
for name, hostvars in host_group("timescaledb").items():
    checks.append(("tcp", f"TimescaleDB {name}", f"{host_addr(name, hostvars)}:{timescaledb_port}"))

minio_port = int(group_vars.get("minio_api_port", 9000))
for name, hostvars in host_group("minio").items():
    checks.append(("http", f"MinIO {name}", f"http://{host_addr(name, hostvars)}:{minio_port}/minio/health/live"))

triton_http_port = int(group_vars.get("triton_http_port", 8000))
for name, hostvars in host_group("triton").items():
    checks.append(("http", f"Triton {name}", f"http://{host_addr(name, hostvars)}:{triton_http_port}/v2/health/ready"))

prometheus_port = int(group_vars.get("prometheus_port", 9090))
grafana_port = int(group_vars.get("grafana_port", 3000))
for name, hostvars in host_group("monitoring").items():
    host = host_addr(name, hostvars)
    checks.append(("http", f"Prometheus {name}", f"http://{host}:{prometheus_port}/-/healthy"))
    checks.append(("http", f"Grafana {name}", f"http://{host}:{grafana_port}/api/health"))

edge_metrics_port = int(group_vars.get("edge_gateway_metrics_port", 9090))
for name, hostvars in host_group("edge_gateways").items():
    host = host_addr(name, hostvars)
    checks.append(("http", f"Edge Agent {name}", f"http://{host}:{edge_metrics_port}/metrics"))

mtmc_default_port = 8080
for name, hostvars in host_group("services").items():
    host = host_addr(name, hostvars)
    if hostvars.get("mtmc_enabled", True):
        checks.append(
            (
                "http",
                f"MTMC {name}",
                f"http://{host}:{int(hostvars.get('mtmc_host_metrics_port', mtmc_default_port))}/metrics",
            )
        )
    for deployment in hostvars.get("service_deployments", []) or []:
        smoke_url = deployment.get("health_check_url") or deployment.get("smoke_test_url")
        if not smoke_url:
            continue
        checks.append(
            (
                "http",
                f"Service {name} {deployment.get('name', 'app')}",
                externalize_local_url(str(smoke_url), host),
            )
        )

for kind, name, target in checks:
    print(f"{kind}|{name}|{target}")
PY
}

perform_http_check() {
  local url="$1"
  curl --silent --show-error --fail --max-time 10 "$url" >/dev/null
}

perform_tcp_check() {
  local host="$1"
  local port="$2"
  timeout 5 bash -c ">/dev/tcp/${host}/${port}" >/dev/null 2>&1
}

run_check() {
  local kind="$1"
  local target="$2"
  if [[ "$kind" == "http" ]]; then
    perform_http_check "$target"
  else
    local host="${target%%:*}"
    local port="${target##*:}"
    perform_tcp_check "$host" "$port"
  fi
}

if [[ -n "$INVENTORY_PATH" ]]; then
  while IFS='|' read -r kind name target; do
    [[ -z "${kind:-}" ]] && continue
    append_check "$kind" "$name" "$target"
  done < <(load_inventory_checks "$INVENTORY_PATH")
fi

append_csv_tcp_checks "Kafka" "${KAFKA_HOSTS:-}" "${KAFKA_PORT:-9093}"
append_csv_http_checks "NATS" "${NATS_HOSTS:-}" "${NATS_HTTP_PORT:-8222}" "/healthz"
append_csv_tcp_checks "TimescaleDB" "${TIMESCALEDB_HOSTS:-}" "${TIMESCALEDB_PORT:-5432}"
append_csv_http_checks "MinIO" "${MINIO_HOSTS:-}" "${MINIO_PORT:-9000}" "/minio/health/live"
append_csv_http_checks "Triton" "${TRITON_HOSTS:-}" "${TRITON_HTTP_PORT:-8000}" "/v2/health/ready"
append_csv_http_checks "Prometheus" "${PROMETHEUS_HOSTS:-}" "${PROMETHEUS_PORT:-9090}" "/-/healthy"
append_csv_http_checks "Grafana" "${GRAFANA_HOSTS:-}" "${GRAFANA_PORT:-3000}" "/api/health"
append_csv_http_checks "Edge Agent" "${EDGE_AGENT_HOSTS:-}" "${EDGE_AGENT_PORT:-9090}" "/metrics"
append_app_endpoints "${APP_ENDPOINTS:-}"

# Auto-detect local docker-compose when no other config provided
if [[ "${#CHECK_NAMES[@]}" -eq 0 ]]; then
  if [[ "$LOCAL_MODE" -eq 1 ]] || docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^kafka-0$'; then
    printf '%sAuto-detected local docker-compose stack%s\n\n' "$COLOR_GREEN" "$COLOR_RESET"
    append_check "tcp" "Kafka 1" "localhost:19092"
    append_check "tcp" "Kafka 2" "localhost:19093"
    append_check "tcp" "Kafka 3" "localhost:19094"
    append_check "http" "NATS" "http://localhost:8222/healthz"
    append_check "tcp" "TimescaleDB" "localhost:5432"
    append_check "http" "MinIO" "http://localhost:9000/minio/health/live"
    append_check "tcp" "Redis" "localhost:6379"
    append_check "http" "Prometheus" "http://localhost:9090/-/healthy"
    append_check "http" "Grafana" "http://localhost:3000/api/health"
  fi
fi

if [[ "${#CHECK_NAMES[@]}" -eq 0 ]]; then
  printf '%sNo checks configured.%s Use --inventory, --local, or set host env vars.\n' "$COLOR_YELLOW" "$COLOR_RESET" >&2
  exit 1
fi

printf '%-40s %-6s %s\n' "Component" "State" "Target"
printf '%-40s %-6s %s\n' "---------" "-----" "------"

failed=0
for idx in "${!CHECK_NAMES[@]}"; do
  name="${CHECK_NAMES[$idx]}"
  kind="${CHECK_KINDS[$idx]}"
  target="${CHECK_TARGETS[$idx]}"
  if run_check "$kind" "$target"; then
    CHECK_RESULTS+=("PASS")
    printf '%-40s %s%-6s%s %s\n' "$name" "$COLOR_GREEN" "PASS" "$COLOR_RESET" "$target"
  else
    CHECK_RESULTS+=("FAIL")
    failed=$((failed + 1))
    printf '%-40s %s%-6s%s %s\n' "$name" "$COLOR_RED" "FAIL" "$COLOR_RESET" "$target"
  fi
done

passed=$(( ${#CHECK_NAMES[@]} - failed ))
printf '\n%-40s %s/%s passed\n' "Summary" "$passed" "${#CHECK_NAMES[@]}"

if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
