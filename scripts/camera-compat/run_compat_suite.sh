#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROBE_SCRIPT="${SCRIPT_DIR}/probe_camera.py"

usage() {
  cat <<'EOF'
Usage:
  scripts/camera-compat/run_compat_suite.sh CAMERA_LIST.[csv|yaml|yml] [OUTPUT_DIR]

The camera list may contain these columns/keys:
  brand, model, host, onvif_port, username, password, rtsp_url,
  firmware, ik10, smart_codec, ir_detect, onvif_summary, rtsp_summary,
  h265, dual_stream, triple_stream, status, published_only, notes, sources

Notes:
  - CSV uses comma-separated columns with the names above.
  - YAML may be either a list of camera objects or an object with `cameras: [...]`.
  - `published_only: true` lets you seed the matrix from datasheets when no camera
    endpoint is available. Live rows are still probed through probe_camera.py.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage >&2
  exit 1
fi

CAMERA_LIST="$1"
OUTPUT_DIR="${2:-${REPO_ROOT}/artifacts/camera-compat/latest}"
REPORT_DIR="${OUTPUT_DIR}/reports"
MATRIX_PATH="${OUTPUT_DIR}/matrix.md"
INVENTORY_JSONL="$(mktemp)"

mkdir -p "${REPORT_DIR}"
trap 'rm -f "${INVENTORY_JSONL}"' EXIT

python3 - "${CAMERA_LIST}" > "${INVENTORY_JSONL}" <<'PY'
import csv
import json
import sys
from pathlib import Path


def load_rows(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            yield from csv.DictReader(handle)
        return

    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit("YAML inventory requires PyYAML; install pyyaml") from exc

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(payload, dict):
            rows = payload.get("cameras")
            if rows is None:
                raise SystemExit("YAML inventory must be a list or contain a top-level 'cameras' key")
            payload = rows
        if not isinstance(payload, list):
            raise SystemExit("YAML inventory must decode to a list of camera rows")
        for row in payload:
            if not isinstance(row, dict):
                raise SystemExit("Each YAML camera row must be a mapping")
            yield row
        return

    raise SystemExit(f"Unsupported inventory format: {path.suffix}")


inventory_path = Path(sys.argv[1])
for row in load_rows(inventory_path):
    normalized = {str(k): v for k, v in row.items()}
    print(json.dumps(normalized))
PY

while IFS= read -r row_json; do
  slug="$(python3 - "${row_json}" <<'PY'
import json
import re
import sys

row = json.loads(sys.argv[1])
value = f"{row.get('brand', 'camera')}-{row.get('model', 'unknown')}"
print(re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-"))
PY
)"
  report_path="${REPORT_DIR}/${slug}.json"

  mapfile -t probe_args < <(python3 - "${row_json}" "${report_path}" <<'PY'
import json
import sys

row = json.loads(sys.argv[1])
report_path = sys.argv[2]

args = [
    "--brand", str(row["brand"]),
    "--model", str(row["model"]),
    "--output", report_path,
]

single_value_fields = {
    "host": "--host",
    "onvif_port": "--onvif-port",
    "username": "--username",
    "password": "--password",
    "rtsp_url": "--rtsp-url",
    "firmware": "--firmware",
    "ik10": "--ik10",
    "smart_codec": "--smart-codec",
    "ir_detect": "--ir-detect",
    "onvif_summary": "--onvif-summary",
    "rtsp_summary": "--rtsp-summary",
    "h265": "--h265",
    "dual_stream": "--dual-stream",
    "triple_stream": "--triple-stream",
    "status": "--status",
}

for field, flag in single_value_fields.items():
    value = row.get(field)
    if value is None or value == "":
        continue
    if isinstance(value, bool):
        if field in {"h265", "dual_stream", "triple_stream"}:
            value = "yes" if value else "no"
        else:
            value = "Yes" if value else "No"
    args.extend([flag, str(value)])

published_only = row.get("published_only")
if isinstance(published_only, str):
    published_only = published_only.strip().lower() in {"1", "true", "yes", "y"}
if published_only:
    args.append("--published-only")

for key, flag in (("notes", "--note"), ("sources", "--source")):
    value = row.get(key, [])
    if value in (None, ""):
        continue
    if not isinstance(value, list):
        value = [value]
    for item in value:
        args.extend([flag, str(item)])

for item in args:
    print(item)
PY
  )

  python3 "${PROBE_SCRIPT}" "${probe_args[@]}"
done < "${INVENTORY_JSONL}"

python3 - "${REPORT_DIR}" "${MATRIX_PATH}" <<'PY'
import json
import sys
from pathlib import Path

report_dir = Path(sys.argv[1])
matrix_path = Path(sys.argv[2])
reports = []
for report_path in sorted(report_dir.glob("*.json")):
    reports.append(json.loads(report_path.read_text(encoding="utf-8")))

columns = [
    "Brand",
    "Model",
    "Firmware",
    "ONVIF",
    "RTSP",
    "H.265",
    "Dual Stream",
    "Triple Stream",
    "IR Detect",
    "IK10",
    "Smart Codec",
    "Status",
]

lines = []
lines.append("# Camera Compatibility Matrix")
lines.append("")
lines.append(f"Generated from `{report_dir}`.")
lines.append("")
lines.append("| " + " | ".join(columns) + " |")
lines.append("|" + "|".join(["---"] * len(columns)) + "|")
for report in reports:
    row = report.get("matrix_columns", {})
    cells = [str(row.get(column, "Unknown")).replace("\n", " ") for column in columns]
    lines.append("| " + " | ".join(cells) + " |")

if reports:
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for report in reports:
        label = f"{report.get('brand', 'Unknown')} {report.get('model', 'Unknown')}"
        notes = report.get("notes") or []
        sources = report.get("sources") or []
        if not notes and not sources:
            continue
        lines.append(f"### {label}")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        for source in sources:
            lines.append(f"- Source: {source}")
        lines.append("")

matrix_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
PY

printf 'Wrote JSON reports to %s\n' "${REPORT_DIR}"
printf 'Wrote matrix to %s\n' "${MATRIX_PATH}"
