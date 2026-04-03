#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Launcher ──────────────────────────────────────────
# Usage: .agents/launch.sh <task-id>
# Example: .agents/launch.sh P1-V01
#
# This script:
# 1. Reads the task from manifest.yaml
# 2. Checks all dependencies are status: done
# 3. Creates a Git branch feat/{task-id}
# 4. Combines role config + task prompt into .claude-task-context.md
# 5. Updates manifest status to in_progress
# 6. Tells you how to start the agent
# ──────────────────────────────────────────────────────────────

TASK_ID="${1:?Usage: .agents/launch.sh <task-id>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="${REPO_ROOT}/.agents/manifest.yaml"
ROLES_DIR="${REPO_ROOT}/.agents/roles"

echo "🔍 Looking up task ${TASK_ID}..."

# ── Parse task from manifest (no eval — outputs one value per line) ──
TASK_DATA=$(python3 - "${MANIFEST}" "${TASK_ID}" << 'PYEOF'
import yaml, sys, json

manifest_path = sys.argv[1]
task_id = sys.argv[2]

with open(manifest_path) as f:
    m = yaml.safe_load(f)

for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == task_id:
            print(task["role"])
            print(task["title"])
            print(task["tool"])
            print(task["prompt"])
            print(task["status"])
            print(json.dumps(task.get("depends_on") or []))
            sys.exit(0)

print("NOT_FOUND")
sys.exit(1)
PYEOF
)

# Read values by line number (no eval, no special char issues)
ROLE=$(echo "$TASK_DATA" | sed -n '1p')
TITLE=$(echo "$TASK_DATA" | sed -n '2p')
TOOL=$(echo "$TASK_DATA" | sed -n '3p')
PROMPT=$(echo "$TASK_DATA" | sed -n '4p')
STATUS=$(echo "$TASK_DATA" | sed -n '5p')
DEPENDS_JSON=$(echo "$TASK_DATA" | sed -n '6p')

if [ "$ROLE" = "NOT_FOUND" ] || [ -z "$ROLE" ]; then
    echo "❌ Task ${TASK_ID} not found in manifest"
    exit 1
fi

echo "📋 Task: ${TASK_ID} — ${TITLE}"
echo "   Role: ${ROLE} | Tool: ${TOOL} | Status: ${STATUS}"

# ── Check status ─────────────────────────────────────────────
if [ "$STATUS" = "done" ]; then
    echo "✅ Already done."
    exit 0
fi
if [ "$STATUS" = "in_progress" ]; then
    echo "⚠️  Task already in progress."
    echo "   To resume: git checkout feat/${TASK_ID} && claude"
    echo "   To restart: update manifest status to 'pending' first."
    exit 1
fi

# ── Check dependencies ───────────────────────────────────────
echo "🔗 Checking dependencies..."

python3 - "${MANIFEST}" "${DEPENDS_JSON}" << 'DEPCHECK'
import yaml, sys, json

manifest_path = sys.argv[1]
depends_json = sys.argv[2]

with open(manifest_path) as f:
    m = yaml.safe_load(f)

status_map = {}
for phase in m["phases"].values():
    for task in phase["tasks"]:
        status_map[task["id"]] = task["status"]

deps = json.loads(depends_json)
if deps:
    blocked = [d for d in deps if status_map.get(d) != "done"]
    if blocked:
        print(f"❌ BLOCKED — these dependencies are not done yet: {blocked}")
        print(f"   Run .agents/status.sh to see what's ready.")
        sys.exit(1)

print("✅ All dependencies satisfied.")
DEPCHECK

# ── Create branch ────────────────────────────────────────────
BRANCH="feat/${TASK_ID}"
echo "🌿 Creating branch: ${BRANCH}"
git checkout main 2>/dev/null || true
git pull --ff-only origin main 2>/dev/null || true
git checkout -b "${BRANCH}" 2>/dev/null || git checkout "${BRANCH}"

# ── Build combined context ───────────────────────────────────
ROLE_FILE="${ROLES_DIR}/${ROLE}.md"
PROMPT_FILE="${REPO_ROOT}/${PROMPT}"
COMBINED="${REPO_ROOT}/.claude-task-context.md"

echo "📝 Building agent context..."

{
    echo "# Task: ${TASK_ID} — ${TITLE}"
    echo "# Role: ${ROLE}"
    echo "# Branch: ${BRANCH}"
    echo ""
    echo "---"
    echo ""
} > "${COMBINED}"

# Append role config
if [ -f "${ROLE_FILE}" ]; then
    cat "${ROLE_FILE}" >> "${COMBINED}"
else
    echo "⚠️  Role file not found: ${ROLE_FILE}" >> "${COMBINED}"
fi

{
    echo ""
    echo "---"
    echo ""
    echo "# Task Prompt"
    echo ""
} >> "${COMBINED}"

# Append task prompt
if [ -f "${PROMPT_FILE}" ]; then
    cat "${PROMPT_FILE}" >> "${COMBINED}"
    echo "   Prompt loaded from: ${PROMPT}"
else
    echo "⚠️  No prompt file at ${PROMPT_FILE}" >> "${COMBINED}"
    echo "   ⚠️  No prompt file found — write prompt manually."
fi

# ── Update manifest status ───────────────────────────────────
python3 - "${MANIFEST}" "${TASK_ID}" "${BRANCH}" << 'PYEOF'
import yaml, sys

manifest_path = sys.argv[1]
task_id = sys.argv[2]
branch = sys.argv[3]

with open(manifest_path) as f:
    m = yaml.safe_load(f)

for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == task_id:
            task["status"] = "in_progress"
            task["branch"] = branch

with open(manifest_path, "w") as f:
    yaml.dump(m, f, default_flow_style=False, sort_keys=False)
PYEOF
echo "📊 Manifest updated: ${TASK_ID} → in_progress"

# ── Print instructions ───────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Ready to launch ${TOOL} for ${TASK_ID}"
echo "  Branch: ${BRANCH}"
echo "  Context: .claude-task-context.md"
echo "════════════════════════════════════════════════════════════"
echo ""

if [ "${TOOL}" = "claude-code" ]; then
    echo "  Start the agent:"
    echo ""
    echo "    claude"
    echo ""
    echo "  Then tell it:"
    echo ""
    echo "    Read .claude-task-context.md and execute the task described in it."
    echo ""
    echo "  When done, review with:"
    echo ""
    echo "    .agents/review.sh ${TASK_ID}"
    echo ""
elif [ "${TOOL}" = "codex-cli" ]; then
    echo "  Start the agent:"
    echo ""
    echo "    codex \"\$(cat .claude-task-context.md)\""
    echo ""
    echo "  When done, review with:"
    echo ""
    echo "    .agents/review.sh ${TASK_ID}"
    echo ""
fi
