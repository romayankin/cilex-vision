#!/usr/bin/env bash
set -euo pipefail

# ─── Auto-update rolling-summary.md ─────────────────────────
# Called automatically after marking a task done.
# Usage: .agents/update-summary.sh [task-id-just-completed]
# ──────────────────────────────────────────────────────────────

REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="${REPO_ROOT}/.agents/manifest.yaml"
SUMMARY="${REPO_ROOT}/.agents/rolling-summary.md"
COMPLETED_TASK="${1:-}"

python3 - "${MANIFEST}" "${REPO_ROOT}" "${COMPLETED_TASK}" << 'PYEOF'
import yaml, sys, os, glob, datetime

manifest_path = sys.argv[1]
repo_root = sys.argv[2]
completed_task = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None

with open(manifest_path) as f:
    m = yaml.safe_load(f)

# Gather all tasks
status_map = {}
all_tasks = []
for pk, pv in m["phases"].items():
    for t in pv["tasks"]:
        status_map[t["id"]] = t["status"]
        all_tasks.append((pk, t))

done = [(p, t) for p, t in all_tasks if t["status"] == "done"]
pending = [(p, t) for p, t in all_tasks if t["status"] == "pending"]
active = [(p, t) for p, t in all_tasks if t["status"] == "in_progress"]

# Find ready tasks
ready = []
for p, t in pending:
    deps = t.get("depends_on") or []
    blocked = [d for d in deps if status_map.get(d) != "done"]
    if not blocked:
        ready.append(t)

# Read latest handoff notes for decisions/constraints
handoff_dir = os.path.join(repo_root, ".agents/handoff")
handoff_files = sorted(glob.glob(os.path.join(handoff_dir, "*.md")), key=os.path.getmtime, reverse=True)

decisions = []
constraints = []
issues = []
for hf in handoff_files[:10]:
    with open(hf) as f:
        content = f.read()
    # Extract key decisions (lines starting with - or * that mention "decision", "chose", "selected", "pattern")
    for line in content.split("\n"):
        ls = line.strip()
        if ls.startswith(("- ", "* ")):
            ll = ls.lower()
            if any(w in ll for w in ["decision", "chose", "selected", "pattern", "convention", "established"]):
                decisions.append(ls[2:].strip())
            if any(w in ll for w in ["constraint", "must not", "never", "critical", "do not"]):
                constraints.append(ls[2:].strip())
            if any(w in ll for w in ["issue", "gap", "todo", "limitation", "missing", "workaround"]):
                issues.append(ls[2:].strip())

# Read todo file for open issues
todo_path = os.path.join(repo_root, "todo_before_deployment.md")
open_todos = []
if os.path.exists(todo_path):
    with open(todo_path) as f:
        for line in f:
            if line.strip().startswith("- [ ]"):
                open_todos.append(line.strip()[6:].strip())

# Determine current goal
current_phase = None
for pk in ["phase-2", "phase-3", "phase-4"]:
    if pk in m["phases"]:
        tasks = m["phases"][pk]["tasks"]
        if any(t["status"] != "done" for t in tasks):
            current_phase = m["phases"][pk]
            current_phase_key = pk
            break

# Find critical path (ready tasks that unblock the most)
def count_downstream(tid):
    count = 0
    for _, t in all_tasks:
        if tid in (t.get("depends_on") or []):
            if t["status"] == "pending":
                count += 1
    return count

ready_ranked = sorted(ready, key=lambda t: count_downstream(t["id"]), reverse=True)

# Build summary
now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
lines = []
lines.append("# Rolling Summary")
lines.append("")
lines.append(f"*Auto-generated after each task. Last updated: {now}" + (f" (after {completed_task})" if completed_task else "") + "*")
lines.append("")

# Current Goal
lines.append("## Current Goal")
lines.append("")
if current_phase:
    phase_done = sum(1 for t in current_phase["tasks"] if t["status"] == "done")
    phase_total = len(current_phase["tasks"])
    lines.append(f'Complete {current_phase["name"]} ({current_phase_key.replace("-", " ").title()}) — {phase_done}/{phase_total} tasks done. '
                 f'Overall progress: {len(done)}/{len(all_tasks)} tasks complete across all phases.')
else:
    lines.append(f"All phases complete. {len(done)}/{len(all_tasks)} tasks done.")
lines.append("")

# Active Constraints (from CONVENTIONS.md + hardcoded essentials)
lines.append("## Active Constraints")
lines.append("")
lines.append("- No image bytes on Kafka — only URI references to MinIO.")
lines.append("- asyncpg COPY protocol for all bulk DB writes, never row-by-row INSERT.")
lines.append("- Three timestamps on every message: source_capture_ts, edge_receive_ts (primary), core_ingest_ts.")
lines.append("- Embedding version boundaries — MTMC never compares across model versions.")
lines.append("- Triton EXPLICIT mode — shadow deploy before cutover.")
lines.append("- Protobuf for all inter-service messages, buf lint in CI.")
lines.append("- Python str enums as TEXT with CHECK constraints, not native PG ENUMs.")
# Add any discovered constraints
for c in constraints[:3]:
    if len(c) < 120:
        lines.append(f"- {c}")
lines.append("")

# Key Decisions
lines.append("## Key Decisions")
lines.append("")
lines.append("- ByteTrack selected as tracker (proxy bake-off on MOT17, live re-validation pending).")
lines.append("- FAISS flat index for real-time MTMC (30-min horizon), pgvector for historical (90 days).")
lines.append("- CPU-only pilot: YOLOv8n ONNX on Triton, 4 cameras, single Ubuntu node.")
for d in decisions[:4]:
    if len(d) < 120:
        lines.append(f"- {d}")
lines.append("")

# Open Issues
lines.append("## Open Issues")
lines.append("")
for t in open_todos[:5]:
    lines.append(f"- {t}")
if not open_todos:
    lines.append("- None tracked.")
lines.append("")

# Next Steps
lines.append("## Next Steps")
lines.append("")
if active:
    lines.append(f"{len(active)} task(s) in progress:")
    for _, t in active:
        lines.append(f"- **{t['id']}** ({t['title']}) — branch: {t.get('branch', '?')}")
    lines.append("")

lines.append(f"{len(ready_ranked)} task(s) ready to launch. Priority:")
for t in ready_ranked[:6]:
    ds = count_downstream(t["id"])
    note = f" — unblocks {ds} tasks" if ds > 0 else ""
    lines.append(f"- **{t['id']}** ({t['title']}) → {t['tool']}{note}")
lines.append("")

output = "\n".join(lines)

# Trim to ~500 words max
words = output.split()
if len(words) > 520:
    # Truncate open issues and next steps
    output = "\n".join(lines[:len(lines)-2])

summary_path = os.path.join(repo_root, ".agents/rolling-summary.md")
with open(summary_path, "w") as f:
    f.write(output + "\n")

print(f"Updated .agents/rolling-summary.md ({len(output.split())} words)")
PYEOF
