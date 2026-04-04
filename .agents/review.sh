#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Output Quality Review ─────────────────────────────
# Usage: .agents/review.sh <task-id>
# Runs automated checks and produces a quality scorecard.
#
# Works in three modes:
# 1. On feat/{task-id} branch → diffs against main (ideal)
# 2. On main with task branch existing → diffs main vs branch
# 3. On main, branch merged → checks files by role expectations
# ──────────────────────────────────────────────────────────────

TASK_ID="${1:?Usage: .agents/review.sh <task-id>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="${REPO_ROOT}/.agents/manifest.yaml"

echo "═══════════════════════════════════════════════════════"
echo "  Quality Review: ${TASK_ID}"
echo "═══════════════════════════════════════════════════════"
echo ""

# Parse task role and prompt path
TASK_DATA=$(python3 - "${MANIFEST}" "${TASK_ID}" << 'PYEOF'
import yaml, sys, json
with open(sys.argv[1]) as f:
    m = yaml.safe_load(f)
for phase in m["phases"].values():
    for task in phase["tasks"]:
        if task["id"] == sys.argv[2]:
            print(task["role"])
            print(task.get("branch") or "")
            print(task["prompt"])
            sys.exit(0)
print("NOT_FOUND")
sys.exit(1)
PYEOF
)

ROLE=$(echo "$TASK_DATA" | sed -n '1p')
TASK_BRANCH=$(echo "$TASK_DATA" | sed -n '2p')
PROMPT_FILE=$(echo "$TASK_DATA" | sed -n '3p')

if [ "$ROLE" = "NOT_FOUND" ]; then
    echo "❌ Task ${TASK_ID} not found in manifest"
    exit 1
fi

echo "Role: ${ROLE}"

# ── Determine diff mode ─────────────────────────────────────
CURRENT_BRANCH=$(git branch --show-current)
CHANGED_FILES=""

if [ "$CURRENT_BRANCH" = "feat/${TASK_ID}" ]; then
    # Mode 1: We're on the feature branch — diff against main
    echo "Branch: ${CURRENT_BRANCH} (comparing against main)"
    # Include BOTH committed and uncommitted changes vs main
    COMMITTED=$(git diff --name-only main...HEAD 2>/dev/null || true)
    UNCOMMITTED=$(git diff --name-only main 2>/dev/null || true)
    UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null || true)
    # Combine and deduplicate
    CHANGED_FILES=$(printf "%s\n%s\n%s" "$COMMITTED" "$UNCOMMITTED" "$UNTRACKED" | sort -u | grep -v '^$' || true)

    # Warn if there are uncommitted changes
    UNCOMMITTED_COUNT=$(git status --porcelain 2>/dev/null | wc -l)
    if [ "$UNCOMMITTED_COUNT" -gt 0 ]; then
        echo ""
        echo "  ⚠️  ${UNCOMMITTED_COUNT} uncommitted changes detected."
        echo "     Consider: git add -A && git commit -m '${TASK_ID}: <description>'"
        echo ""
    fi
elif git rev-parse --verify "feat/${TASK_ID}" >/dev/null 2>&1; then
    # Mode 2: Feature branch exists but we're on main — diff main vs branch
    echo "Branch: feat/${TASK_ID} (exists, comparing from main)"
    CHANGED_FILES=$(git diff --name-only main...feat/${TASK_ID} 2>/dev/null || true)
else
    # Mode 3: Branch merged or doesn't exist — check expected output files
    echo "Branch: merged to main (checking files directly)"
    echo ""
fi

echo ""

PASS=0
FAIL=0
WARN=0

check_pass() { echo "  ✅ PASS: $1"; PASS=$((PASS+1)); }
check_fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL+1)); }
check_warn() { echo "  ⚠️  WARN: $1"; WARN=$((WARN+1)); }

# ── Universal checks ────────────────────────────────────────
echo "📋 Universal Checks"
echo "───────────────────"

if [ -n "$CHANGED_FILES" ]; then
    CHANGED_COUNT=$(echo "$CHANGED_FILES" | wc -l)
    check_pass "Agent produced ${CHANGED_COUNT} changed files"
else
    # Mode 3: Can't diff, so check if key output files exist and aren't stubs
    echo "  ℹ️  Cannot diff (branch merged). Checking output files directly."

    # Check based on role what files should exist
    FOUND_REAL=0
    case "$ROLE" in
        design)
            # Check docs/ and proto/ for non-stub files
            for f in docs/taxonomy.md docs/kafka-contract.md docs/security-design.md \
                     docs/time-sync-policy.md docs/triton-placement.md; do
                if [ -f "$f" ] && ! grep -q "⚠️ This is a placeholder" "$f" 2>/dev/null; then
                    FOUND_REAL=$((FOUND_REAL+1))
                fi
            done
            # Check proto files
            PROTO_COUNT=$(find proto/ -name "*.proto" 2>/dev/null | wc -l)
            FOUND_REAL=$((FOUND_REAL + PROTO_COUNT))
            ;;
        dev)
            # Check for services with real code (not just .gitkeep)
            for svc in services/*/; do
                if [ -f "${svc}main.py" ]; then
                    FOUND_REAL=$((FOUND_REAL+1))
                fi
            done
            ;;
        ops)
            if [ -f "infra/docker-compose.yml" ]; then
                FOUND_REAL=$((FOUND_REAL+1))
            fi
            PLAYBOOK_COUNT=$(find infra/ansible/playbooks/ -name "*.yml" 2>/dev/null | wc -l)
            FOUND_REAL=$((FOUND_REAL + PLAYBOOK_COUNT))
            ;;
        eval)
            SCRIPT_COUNT=$(find scripts/bakeoff/ -name "*.py" 2>/dev/null | wc -l)
            FOUND_REAL=$((FOUND_REAL + SCRIPT_COUNT))
            ;;
        data)
            SCRIPT_COUNT=$(find scripts/annotation/ -name "*.py" 2>/dev/null | wc -l)
            FOUND_REAL=$((FOUND_REAL + SCRIPT_COUNT))
            ;;
        doc)
            for f in docs/runbooks/*.md docs/guides/*.md; do
                if [ -f "$f" ] && [ "$(basename "$f")" != ".gitkeep" ]; then
                    FOUND_REAL=$((FOUND_REAL+1))
                fi
            done
            ;;
    esac

    if [ "$FOUND_REAL" -gt 0 ]; then
        check_pass "Found ${FOUND_REAL} output files for ${ROLE} role"
    else
        check_fail "No output files found for ${ROLE} role"
    fi
fi

# Check write-zone compliance (only if we have a diff)
if [ -n "$CHANGED_FILES" ]; then
    case "$ROLE" in
        dev)
            VIOLATIONS=$(echo "$CHANGED_FILES" | grep -v "^services/" | grep -v "^tests/" | grep -v "^\.agents/" || true)
            [ -z "$VIOLATIONS" ] && check_pass "All changes within DEV write zone" || check_fail "Changes OUTSIDE write zone: ${VIOLATIONS}"
            ;;
        ops)
            VIOLATIONS=$(echo "$CHANGED_FILES" | grep -v "^infra/" | grep -v "^\.github/" | grep -v "^\.agents/" || true)
            [ -z "$VIOLATIONS" ] && check_pass "All changes within OPS write zone" || check_warn "Changes outside primary zone: ${VIOLATIONS}"
            ;;
        design)
            VIOLATIONS=$(echo "$CHANGED_FILES" | grep -v "^docs/" | grep -v "^proto/" | grep -v "^services/db/" | grep -v "^services/monitoring/" | grep -v "^services/topology/" | grep -v "^infra/" | grep -v "^\.github/" | grep -v "^\.agents/" || true)
            [ -z "$VIOLATIONS" ] && check_pass "All changes within DESIGN write zone" || check_warn "Changes outside primary zone: ${VIOLATIONS}"
            ;;
        *)
            check_pass "Write zone check skipped for ${ROLE} role"
            ;;
    esac
fi

echo ""

# ── Role-specific checks ────────────────────────────────────

if [ "$ROLE" = "design" ]; then
    echo "📋 DESIGN Role Checks"
    echo "───────────────────"

    # Check specific outputs based on task ID
    case "$TASK_ID" in
        P0-D01)
            if [ -f "docs/taxonomy.md" ]; then
                LINES=$(wc -l < docs/taxonomy.md)
                [ "$LINES" -gt 50 ] && check_pass "taxonomy.md has ${LINES} lines (substantial)" || check_warn "taxonomy.md only ${LINES} lines (expected >50)"
                grep -q "⚠️ This is a placeholder" docs/taxonomy.md && check_fail "taxonomy.md still contains placeholder — stub not replaced" || check_pass "Stub placeholder removed"
                grep -qi "confidence" docs/taxonomy.md && check_pass "Contains confidence thresholds" || check_warn "No confidence thresholds found"
                grep -qi "NFR\|non.functional\|latency\|retention" docs/taxonomy.md && check_pass "Contains NFR section" || check_fail "No NFR section found"
                grep -qi "mermaid\|state.*diagram\|stateDiagram" docs/taxonomy.md && check_pass "Contains state machine diagram" || check_warn "No Mermaid state diagram found"
            else
                check_fail "docs/taxonomy.md does not exist"
            fi
            ;;
        P0-D02)
            PROTO_COUNT=$(find proto/ -name "*.proto" -not -name "*.gitkeep" 2>/dev/null | wc -l)
            [ "$PROTO_COUNT" -ge 5 ] && check_pass "Found ${PROTO_COUNT} .proto files" || check_fail "Only ${PROTO_COUNT} .proto files (expected >=5)"
            [ -f "proto/buf.yaml" ] && check_pass "buf.yaml exists" || check_fail "No buf.yaml"
            [ -f "proto/README.md" ] && check_pass "proto/README.md exists" || check_warn "No proto/README.md"
            # Try buf lint
            if command -v buf >/dev/null 2>&1; then
                (cd proto && buf lint 2>/dev/null) && check_pass "buf lint passes" || check_warn "buf lint has issues"
            else
                check_warn "buf not installed — skipping lint check"
            fi
            ;;
        P0-D03)
            [ -f "infra/kafka/topics.yaml" ] && check_pass "topics.yaml exists" || check_fail "No topics.yaml"
            [ -f "docs/kafka-contract.md" ] && ! grep -q "⚠️ This is a placeholder" docs/kafka-contract.md && check_pass "kafka-contract.md stub replaced" || check_fail "kafka-contract.md missing or still stub"
            ;;
        *)
            # Generic design checks
            for f in $(echo "$CHANGED_FILES" | grep "^docs/.*\.md$" 2>/dev/null || true); do
                if [ -f "$f" ]; then
                    grep -q "⚠️ This is a placeholder" "$f" && check_fail "${f} still contains placeholder" || check_pass "${f} — content looks real"
                fi
            done
            ;;
    esac
fi

if [ "$ROLE" = "dev" ]; then
    echo "📋 DEV Role Checks"
    echo "───────────────────"

    # Find service directory from changed files or task ID
    if [ -n "$CHANGED_FILES" ]; then
        SVC_DIR=$(echo "$CHANGED_FILES" | grep "^services/" | head -1 | cut -d/ -f1-2)
    else
        # Guess from task ID
        case "$TASK_ID" in
            P1-V01) SVC_DIR="services/edge-agent" ;;
            P1-V02) SVC_DIR="services/ingress-bridge" ;;
            P1-V03) SVC_DIR="services/decode-service" ;;
            P1-V04) SVC_DIR="services/inference-worker" ;;
            P1-V05) SVC_DIR="services/bulk-collector" ;;
            P1-V06) SVC_DIR="services/query-api" ;;
            *) SVC_DIR="" ;;
        esac
    fi

    if [ -n "$SVC_DIR" ] && [ -d "$SVC_DIR" ]; then
        echo "  Service: ${SVC_DIR}"
        echo ""

        # main.py exists
        [ -f "${SVC_DIR}/main.py" ] && check_pass "main.py exists" || check_fail "No main.py"

        # Dockerfile
        if [ -f "${SVC_DIR}/Dockerfile" ]; then
            check_pass "Dockerfile exists"
            docker build -q "${SVC_DIR}" > /dev/null 2>&1 && check_pass "Docker build succeeds" || check_warn "Docker build failed (may need running dependencies)"
        else
            check_fail "No Dockerfile"
        fi

        # Tests
        if [ -d "${SVC_DIR}/tests" ] || ls "${SVC_DIR}"/test_*.py 1>/dev/null 2>&1; then
            check_pass "Tests exist"
            (cd "${SVC_DIR}" && python3 -m pytest tests/ -q 2>/dev/null) && check_pass "Tests pass" || check_warn "Some tests failed (may need running services)"
        else
            check_fail "No tests found"
        fi

        # requirements.txt
        [ -f "${SVC_DIR}/requirements.txt" ] && check_pass "requirements.txt exists" || check_warn "No requirements.txt"

        # config.py with Pydantic
        if [ -f "${SVC_DIR}/config.py" ]; then
            grep -q "pydantic\|BaseSettings\|BaseModel" "${SVC_DIR}/config.py" && check_pass "Config uses Pydantic" || check_warn "Config exists but doesn't use Pydantic"
        else
            check_warn "No config.py"
        fi

        # Prometheus metrics
        grep -rq "prometheus_client\|Counter\|Histogram\|Gauge" "${SVC_DIR}/" 2>/dev/null && check_pass "Prometheus metrics found" || check_fail "No Prometheus metrics"

        # No row-by-row INSERT
        if grep -rq "\.execute.*INSERT" "${SVC_DIR}/" --include="*.py" 2>/dev/null; then
            check_fail "Found row-by-row INSERT — must use asyncpg COPY"
        else
            check_pass "No row-by-row INSERT found"
        fi

        # Lint
        ruff check "${SVC_DIR}/" 2>/dev/null && check_pass "ruff lint passes" || check_warn "Lint issues found (run: ruff check ${SVC_DIR}/)"
    else
        check_warn "Could not determine service directory for ${TASK_ID}"
    fi
fi

if [ "$ROLE" = "ops" ]; then
    echo "📋 OPS Role Checks"
    echo "───────────────────"

    [ -f "infra/docker-compose.yml" ] && check_pass "docker-compose.yml exists" || check_fail "No docker-compose.yml"

    if [ -f "infra/docker-compose.yml" ]; then
        docker-compose -f infra/docker-compose.yml config -q 2>/dev/null && check_pass "docker-compose validates" || check_warn "docker-compose validation issues"
    fi

    # YAML validity
    for f in $(find infra/ -name "*.yml" -o -name "*.yaml" 2>/dev/null | head -20); do
        python3 -c "import yaml; yaml.safe_load(open('$f'))" 2>/dev/null && true || check_fail "Invalid YAML: ${f}"
    done
    check_pass "YAML files validated"

    # Check for CI pipeline
    [ -f ".github/workflows/ci.yml" ] && check_pass "CI pipeline exists" || check_warn "No CI pipeline yet"
fi

if [ "$ROLE" = "eval" ]; then
    echo "📋 EVAL Role Checks"
    echo "───────────────────"

    for f in $(find scripts/bakeoff/ scripts/load-test/ scripts/calibration/ -name "*.py" 2>/dev/null); do
        python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null && check_pass "Compiles: $(basename $f)" || check_fail "Syntax error: $(basename $f)"
    done

    grep -rq "mlflow" scripts/bakeoff/ scripts/load-test/ 2>/dev/null && check_pass "MLflow logging found" || check_warn "No MLflow logging"
fi

if [ "$ROLE" = "data" ]; then
    echo "📋 DATA Role Checks"
    echo "───────────────────"

    for f in $(find scripts/annotation/ scripts/data/ -name "*.py" 2>/dev/null); do
        python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null && check_pass "Compiles: $(basename $f)" || check_fail "Syntax error: $(basename $f)"
    done

    [ -f "docs/annotation-guidelines.md" ] && ! grep -q "⚠️ This is a placeholder" docs/annotation-guidelines.md && check_pass "Annotation guidelines written" || check_warn "Annotation guidelines still a stub"
fi

if [ "$ROLE" = "doc" ]; then
    echo "📋 DOC Role Checks"
    echo "───────────────────"

    DOC_COUNT=$(find docs/ -name "*.md" -not -name ".gitkeep" 2>/dev/null | wc -l)
    check_pass "Found ${DOC_COUNT} documentation files"

    # Check for replaced stubs
    STUB_COUNT=$(grep -rl "⚠️ This is a placeholder" docs/ 2>/dev/null | wc -l)
    [ "$STUB_COUNT" -eq 0 ] && check_pass "No remaining stub files" || check_warn "${STUB_COUNT} stub files still need content"
fi

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
TOTAL=$((PASS + FAIL + WARN))
echo "  Score: ${PASS}/${TOTAL} passed | ${FAIL} failed | ${WARN} warnings"
echo ""
if [ $FAIL -eq 0 ] && [ $WARN -le 2 ]; then
    echo "  🟢 RESULT: PASS — proceed to human review"
    echo "     Use: .agents/review-checklists/${ROLE}-review.md"
elif [ $FAIL -eq 0 ]; then
    echo "  🟢 RESULT: PASS (with warnings) — proceed to human review"
    echo "     Address warnings if important, then check:"
    echo "     .agents/review-checklists/${ROLE}-review.md"
elif [ $FAIL -le 2 ]; then
    echo "  🟡 RESULT: NEEDS FIXES — address ${FAIL} FAIL items, then re-run"
    echo ""
    echo "  To fix: paste each ❌ FAIL item to the agent as specific feedback."
else
    echo "  🔴 RESULT: SIGNIFICANT ISSUES — ${FAIL} failures"
    echo ""
    echo "  If the agent is on the wrong track, restart with a clearer prompt."
    echo "  If it's close, paste each ❌ FAIL item as specific feedback."
fi
echo "═══════════════════════════════════════════════════════"
