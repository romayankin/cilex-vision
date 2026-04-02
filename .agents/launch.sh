#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Launcher ──────────────────────────────────────────
# Usage: .agents/launch.sh <task-id>
# Example: .agents/launch.sh P1-V01

TASK_ID="${1:?Usage: .agents/launch.sh <task-id>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="${REPO_ROOT}/.agents/manifest.yaml"
ROLES_DIR="${REPO_ROOT}/.agents/roles"
PROMPTS_DIR="${REPO_ROOT}/.agents/prompts"

echo "🔍 Looking up task ${TASK_ID}..."

# Parse task from manifest
TASK_INFO=$(python3 << PYEOF
import yaml, sys
with open("${MANIFEST}") as f:
    m = yaml.safe_load(f)
for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == "${TASK_ID}":
            print(f"ROLE={task['role']}")
            print(f"TITLE={task['title']}")
            print(f"TOOL={task['tool']}")
            print(f"PROMPT={task['prompt']}")
            print(f"STATUS={task['status']}")
            deps = task.get("depends_on", [])
            print(f"DEPENDS={deps}")
            sys.exit(0)
print("ERROR=not_found")
sys.exit(1)
PYEOF
)

eval "$TASK_INFO"

if [ "${ERROR:-}" = "not_found" ]; then
    echo "❌ Task ${TASK_ID} not found in manifest"
    exit 1
fi

echo "📋 Task: ${TASK_ID} — ${TITLE}"
echo "   Role: ${ROLE} | Tool: ${TOOL} | Status: ${STATUS}"

# Check status
if [ "$STATUS" = "done" ]; then
    echo "✅ Already done."
    exit 0
fi

# Check dependencies
python3 << DEPCHECK
import yaml, sys, ast
with open("${MANIFEST}") as f:
    m = yaml.safe_load(f)
status_map = {}
for phase in m["phases"].values():
    for task in phase["tasks"]:
        status_map[task["id"]] = task["status"]
deps = ${DEPENDS}
if deps:
    blocked = [d for d in deps if status_map.get(d) != "done"]
    if blocked:
        print(f"❌ BLOCKED — waiting on: {blocked}")
        sys.exit(1)
print("✅ All dependencies satisfied.")
DEPCHECK

# Create branch
BRANCH="feat/${TASK_ID}"
echo "🌿 Creating branch: ${BRANCH}"
git checkout main 2>/dev/null || true
git checkout -b "${BRANCH}" 2>/dev/null || git checkout "${BRANCH}"

# Build combined context
ROLE_FILE="${ROLES_DIR}/${ROLE}.md"
PROMPT_FILE="${REPO_ROOT}/${PROMPT}"
COMBINED="${REPO_ROOT}/.claude-task-context.md"

echo "# Task: ${TASK_ID} — ${TITLE}" > "${COMBINED}"
echo "# Role: ${ROLE}" >> "${COMBINED}"
echo "# Branch: ${BRANCH}" >> "${COMBINED}"
echo "" >> "${COMBINED}"
echo "---" >> "${COMBINED}"
echo "" >> "${COMBINED}"
cat "${ROLE_FILE}" >> "${COMBINED}"
echo "" >> "${COMBINED}"
echo "---" >> "${COMBINED}"
echo "" >> "${COMBINED}"
echo "# Task Prompt" >> "${COMBINED}"
echo "" >> "${COMBINED}"
if [ -f "${PROMPT_FILE}" ]; then
    cat "${PROMPT_FILE}" >> "${COMBINED}"
else
    echo "⚠️  No prompt file found at ${PROMPT_FILE}"
    echo "Write your prompt here or paste from the project plan." >> "${COMBINED}"
fi

# Update manifest
python3 << PYEOF
import yaml
with open("${MANIFEST}") as f:
    m = yaml.safe_load(f)
for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == "${TASK_ID}":
            task["status"] = "in_progress"
            task["branch"] = "${BRANCH}"
with open("${MANIFEST}", "w") as f:
    yaml.dump(m, f, default_flow_style=False, sort_keys=False)
PYEOF

echo ""
echo "════════════════════════════════════════════════════"
echo "  Ready to launch ${TOOL} for ${TASK_ID}"
echo "  Context saved to: .claude-task-context.md"
echo "════════════════════════════════════════════════════"
echo ""

if [ "${TOOL}" = "claude-code" ]; then
    echo "→ Run: claude"
    echo "→ Then tell it: Read .claude-task-context.md and execute the task."
elif [ "${TOOL}" = "codex-cli" ]; then
    echo '→ Run: codex "$(cat .claude-task-context.md)"'
fi
