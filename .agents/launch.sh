#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Launcher ──────────────────────────────────────────
# Usage: .agents/launch.sh <task-id>
# Example: .agents/launch.sh P1-V01
#
# Creates branch, builds context (role + handoff notes + prompt),
# updates manifest, and tells you how to start the agent.
# ──────────────────────────────────────────────────────────────

TASK_ID="${1:?Usage: .agents/launch.sh <task-id>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="${REPO_ROOT}/.agents/manifest.yaml"
ROLES_DIR="${REPO_ROOT}/.agents/roles"
HANDOFF_DIR="${REPO_ROOT}/.agents/handoff"

echo "🔍 Looking up task ${TASK_ID}..."

# ── Parse task from manifest ─────────────────────────────────
TASK_DATA=$(python3 - "${MANIFEST}" "${TASK_ID}" << 'PYEOF'
import yaml, sys, json
with open(sys.argv[1]) as f:
    m = yaml.safe_load(f)
for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == sys.argv[2]:
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
    echo "   To resume: git checkout feat/${TASK_ID}"
    echo "   Then: claude --continue  (Claude Code)"
    echo "   Or:   codex              (Codex CLI — read .claude-task-context.md)"
    exit 1
fi

# ── Check dependencies ───────────────────────────────────────
echo "🔗 Checking dependencies..."

python3 - "${MANIFEST}" "${DEPENDS_JSON}" << 'DEPCHECK'
import yaml, sys, json
with open(sys.argv[1]) as f:
    m = yaml.safe_load(f)
status_map = {}
for phase in m["phases"].values():
    for task in phase["tasks"]:
        status_map[task["id"]] = task["status"]
deps = json.loads(sys.argv[2])
if deps:
    blocked = [d for d in deps if status_map.get(d) != "done"]
    if blocked:
        print(f"❌ BLOCKED — waiting on: {blocked}")
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

# Header
cat > "${COMBINED}" << CTXEOF
# Task: ${TASK_ID} — ${TITLE}
# Role: ${ROLE}
# Tool: ${TOOL}
# Branch: ${BRANCH}

---

# IMPORTANT: Read CONVENTIONS.md first for all established patterns and coding standards.
# It contains patterns established by previous agents (both Claude Code and Codex CLI).

---

CTXEOF

# Role config
if [ -f "${ROLE_FILE}" ]; then
    cat "${ROLE_FILE}" >> "${COMBINED}"
else
    echo "⚠️  Role file not found: ${ROLE_FILE}" >> "${COMBINED}"
fi

# ── Inject handoff notes ─────────────────────────────────────
HANDOFF_COUNT=0

cat >> "${COMBINED}" << 'HDREOF'

---

# Handoff Notes from Previous Tasks
# These contain decisions, patterns, and gotchas from agents that completed earlier tasks.
# Read them for context — they prevent you from making inconsistent choices.

HDREOF

# First: handoff notes from direct dependency tasks
DEP_IDS=$(python3 - "${DEPENDS_JSON}" << 'PYDEPS'
import json, sys
deps = json.loads(sys.argv[1])
for d in deps:
    print(d)
PYDEPS
)

INCLUDED_NOTES=""
for dep_id in $DEP_IDS; do
    if [ -f "${HANDOFF_DIR}/${dep_id}.md" ]; then
        cat "${HANDOFF_DIR}/${dep_id}.md" >> "${COMBINED}"
        echo "" >> "${COMBINED}"
        HANDOFF_COUNT=$((HANDOFF_COUNT+1))
        INCLUDED_NOTES="${INCLUDED_NOTES} ${dep_id}"
    fi
done

# Second: up to 5 most recent handoff notes for cross-task pattern awareness
for note in $(ls -t "${HANDOFF_DIR}"/*.md 2>/dev/null | head -5); do
    note_name=$(basename "$note" .md)
    # Skip if already included as dependency note
    if ! echo "${INCLUDED_NOTES}" | grep -q "${note_name}"; then
        cat "$note" >> "${COMBINED}"
        echo "" >> "${COMBINED}"
        HANDOFF_COUNT=$((HANDOFF_COUNT+1))
    fi
done

if [ $HANDOFF_COUNT -gt 0 ]; then
    echo "   Included ${HANDOFF_COUNT} handoff notes for context"
else
    echo "   No handoff notes yet (early task)"
fi

# Task prompt
cat >> "${COMBINED}" << 'PROMPTHDR'

---

# Task Prompt

PROMPTHDR

if [ -f "${PROMPT_FILE}" ]; then
    cat "${PROMPT_FILE}" >> "${COMBINED}"
    echo "   Prompt loaded from: ${PROMPT}"
else
    echo "⚠️  No prompt file at ${PROMPT_FILE}" >> "${COMBINED}"
    echo "   ⚠️  No prompt file found — write prompt manually."
fi

# Append reminder to write handoff note when done
cat >> "${COMBINED}" << 'HANDOFFREMINDER'

---

# When You Finish This Task

Before declaring done, create a handoff note at .agents/handoff/TASK_ID.md
(replace TASK_ID with your actual task ID) containing:
1. What you built (files created/modified)
2. Key decisions you made and WHY
3. Patterns you established that future agents should follow
4. Gotchas — things that surprised you or that the next agent should watch for
5. Any open questions or known limitations

This note will be automatically injected into the next agent's context.
HANDOFFREMINDER

# ── Update manifest status ───────────────────────────────────
python3 - "${MANIFEST}" "${TASK_ID}" "${BRANCH}" << 'PYEOF'
import yaml, sys
with open(sys.argv[1]) as f:
    m = yaml.safe_load(f)
for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == sys.argv[2]:
            task["status"] = "in_progress"
            task["branch"] = sys.argv[3]
with open(sys.argv[1], "w") as f:
    yaml.dump(m, f, default_flow_style=False, sort_keys=False)
PYEOF
echo "📊 Manifest updated: ${TASK_ID} → in_progress"

# ── Print instructions ───────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Ready to launch ${TOOL} for ${TASK_ID}"
echo "  Branch: ${BRANCH}"
echo "  Context: .claude-task-context.md (includes ${HANDOFF_COUNT} handoff notes)"
echo "════════════════════════════════════════════════════════════"
echo ""

if [ "${TOOL}" = "claude-code" ]; then
    echo "  Start the agent:"
    echo ""
    echo "    claude --model opus"
    echo ""
    echo "  Then tell it:"
    echo ""
    echo "    Read CONVENTIONS.md then .claude-task-context.md and execute the task."
    echo ""
    echo "  When done, review with:"
    echo ""
    echo "    .agents/review.sh ${TASK_ID}"
    echo ""
elif [ "${TOOL}" = "codex-cli" ]; then
    echo "  Start the agent:"
    echo ""
    echo "    codex"
    echo ""
    echo "  Then tell it:"
    echo ""
    echo "    Read CONVENTIONS.md then .claude-task-context.md and execute the task."
    echo ""
    echo "  When done, review with:"
    echo ""
    echo "    .agents/review.sh ${TASK_ID}"
    echo ""
fi
