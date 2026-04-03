#!/usr/bin/env bash
set -euo pipefail

# ─── Agent Output Quality Review ─────────────────────────────
# Usage: .agents/review.sh <task-id>
# Runs automated checks and produces a quality scorecard.
# ──────────────────────────────────────────────────────────────

TASK_ID="${1:?Usage: .agents/review.sh <task-id>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="${REPO_ROOT}/.agents/manifest.yaml"

echo "═══════════════════════════════════════════════════════"
echo "  Quality Review: ${TASK_ID}"
echo "═══════════════════════════════════════════════════════"
echo ""

# Parse task role
ROLE=$(python3 -c "
import yaml
with open('${MANIFEST}') as f:
    m = yaml.safe_load(f)
for phase in m['phases'].values():
    for task in phase['tasks']:
        if task['id'] == '${TASK_ID}':
            print(task['role'])
")

echo "Role: ${ROLE}"
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

# Check that files were actually created/modified
CHANGED=$(git diff --name-only main...HEAD 2>/dev/null | wc -l)
if [ "$CHANGED" -gt 0 ]; then
    check_pass "Agent produced ${CHANGED} changed files"
else
    check_fail "No files changed — agent produced nothing"
fi

# Check no changes outside role's write zone
if [ "$ROLE" = "dev" ]; then
    VIOLATIONS=$(git diff --name-only main...HEAD 2>/dev/null | grep -v "^services/" | grep -v "^tests/" | grep -v "^\.agents/" || true)
    if [ -z "$VIOLATIONS" ]; then
        check_pass "All changes within DEV write zone (services/, tests/)"
    else
        check_fail "Changes OUTSIDE write zone: ${VIOLATIONS}"
    fi
fi
if [ "$ROLE" = "ops" ]; then
    VIOLATIONS=$(git diff --name-only main...HEAD 2>/dev/null | grep -v "^infra/" | grep -v "^\.github/" | grep -v "^\.agents/" || true)
    if [ -z "$VIOLATIONS" ]; then
        check_pass "All changes within OPS write zone (infra/, .github/)"
    else
        check_warn "Changes outside primary zone: ${VIOLATIONS}"
    fi
fi
if [ "$ROLE" = "design" ]; then
    VIOLATIONS=$(git diff --name-only main...HEAD 2>/dev/null | grep -v "^docs/" | grep -v "^proto/" | grep -v "^services/db/" | grep -v "^\.agents/" || true)
    if [ -z "$VIOLATIONS" ]; then
        check_pass "All changes within DESIGN write zone (docs/, proto/)"
    else
        check_fail "Changes OUTSIDE write zone: ${VIOLATIONS}"
    fi
fi

echo ""

# ── Role-specific checks ────────────────────────────────────
if [ "$ROLE" = "dev" ]; then
    echo "📋 DEV Role Checks"
    echo "───────────────────"

    # Find the service directory that was modified
    SVC_DIR=$(git diff --name-only main...HEAD 2>/dev/null | grep "^services/" | head -1 | cut -d/ -f1-2)

    if [ -n "$SVC_DIR" ] && [ -d "$SVC_DIR" ]; then
        # Dockerfile exists
        if [ -f "${SVC_DIR}/Dockerfile" ]; then
            check_pass "Dockerfile exists"
            # Dockerfile builds
            docker build -q "${SVC_DIR}" > /dev/null 2>&1 && check_pass "Docker build succeeds" || check_warn "Docker build failed (may need dependencies)"
        else
            check_fail "No Dockerfile in ${SVC_DIR}"
        fi

        # Tests exist
        if [ -d "${SVC_DIR}/tests" ] || ls "${SVC_DIR}/test_"*.py 1>/dev/null 2>&1; then
            check_pass "Tests exist"
            # Tests pass
            cd "$SVC_DIR"
            python3 -m pytest tests/ -q 2>/dev/null && check_pass "Tests pass" || check_warn "Some tests failed"
            cd "$REPO_ROOT"
        else
            check_fail "No tests found in ${SVC_DIR}"
        fi

        # requirements.txt exists
        [ -f "${SVC_DIR}/requirements.txt" ] && check_pass "requirements.txt exists" || check_warn "No requirements.txt"

        # config.py uses Pydantic
        if [ -f "${SVC_DIR}/config.py" ]; then
            grep -q "pydantic\|BaseSettings\|BaseModel" "${SVC_DIR}/config.py" && check_pass "Config uses Pydantic" || check_warn "Config doesn't use Pydantic Settings"
        else
            check_warn "No config.py (should use Pydantic Settings)"
        fi

        # Prometheus metrics
        grep -rq "prometheus_client\|Counter\|Histogram\|Gauge" "${SVC_DIR}/" 2>/dev/null && check_pass "Prometheus metrics found" || check_fail "No Prometheus metrics (required at /metrics)"

        # No row-by-row INSERT (should use COPY)
        if grep -rq "\.execute.*INSERT" "${SVC_DIR}/" --include="*.py" 2>/dev/null; then
            check_fail "Found row-by-row INSERT — must use asyncpg COPY protocol"
        else
            check_pass "No row-by-row INSERT found (COPY protocol OK)"
        fi

        # Protobuf usage (if service touches Kafka)
        if grep -rq "kafka\|confluent" "${SVC_DIR}/" --include="*.py" 2>/dev/null; then
            grep -rq "protobuf\|proto\|SerializeToString\|ParseFromString" "${SVC_DIR}/" --include="*.py" 2>/dev/null && check_pass "Protobuf serialization for Kafka" || check_warn "Kafka usage found but no Protobuf — check if JSON used instead"
        fi
    else
        check_fail "No service directory found in changes"
    fi

    # Lint check
    echo ""
    echo "📋 Code Quality"
    echo "───────────────────"
    ruff check "${SVC_DIR}/" 2>/dev/null && check_pass "ruff lint passes" || check_warn "Lint issues found"
fi

if [ "$ROLE" = "design" ]; then
    echo "📋 DESIGN Role Checks"
    echo "───────────────────"

    # Check proto files lint
    if ls proto/*.proto 1>/dev/null 2>&1; then
        cd proto && buf lint 2>/dev/null && check_pass "buf lint passes" || check_warn "buf lint issues"
        cd "$REPO_ROOT"
    fi

    # Check stubs were replaced
    for f in $(git diff --name-only main...HEAD 2>/dev/null | grep "^docs/.*\.md$"); do
        if grep -q "⚠️ This is a placeholder" "$f" 2>/dev/null; then
            check_fail "${f} still contains placeholder warning — stub not properly replaced"
        else
            check_pass "${f} — stub replaced with real content"
        fi
    done

    # Check acceptance criteria exist
    for f in $(git diff --name-only main...HEAD 2>/dev/null | grep "^docs/.*\.md$"); do
        if grep -qi "acceptance\|criteria\|validation\|verify" "$f" 2>/dev/null; then
            check_pass "${f} includes acceptance criteria"
        else
            check_warn "${f} — no acceptance criteria found (DESIGN specs should include them)"
        fi
    done
fi

if [ "$ROLE" = "ops" ]; then
    echo "📋 OPS Role Checks"
    echo "───────────────────"

    # docker-compose validates
    if [ -f infra/docker-compose.yml ]; then
        docker-compose -f infra/docker-compose.yml config -q 2>/dev/null && check_pass "docker-compose validates" || check_warn "docker-compose validation issues"
    fi

    # YAML lint
    for f in $(git diff --name-only main...HEAD 2>/dev/null | grep "\.ya\?ml$"); do
        python3 -c "import yaml; yaml.safe_load(open('$f'))" 2>/dev/null && check_pass "Valid YAML: ${f}" || check_fail "Invalid YAML: ${f}"
    done

    # No hardcoded secrets
    if git diff main...HEAD 2>/dev/null | grep -iE "password|secret|api_key|token" | grep -v "^[+-].*#\|^[+-].*placeholder\|^[+-].*REPLACE\|^[+-].*example\|^diff\|^---\|^+++" | head -3; then
        check_warn "Possible hardcoded secrets found — review manually"
    else
        check_pass "No hardcoded secrets detected"
    fi
fi

if [ "$ROLE" = "eval" ]; then
    echo "📋 EVAL Role Checks"
    echo "───────────────────"

    # Scripts are executable
    for f in $(git diff --name-only main...HEAD 2>/dev/null | grep "\.py$"); do
        python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null && check_pass "Compiles: ${f}" || check_fail "Syntax error: ${f}"
    done

    # MLflow logging present
    if git diff main...HEAD 2>/dev/null | grep -q "mlflow"; then
        check_pass "MLflow logging found"
    else
        check_warn "No MLflow logging — EVAL scripts should log all runs"
    fi
fi

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
TOTAL=$((PASS + FAIL + WARN))
echo "  Score: ${PASS}/${TOTAL} passed | ${FAIL} failed | ${WARN} warnings"
echo ""
if [ $FAIL -eq 0 ]; then
    echo "  🟢 RESULT: PASS — proceed to human review"
elif [ $FAIL -le 2 ]; then
    echo "  🟡 RESULT: NEEDS FIXES — address FAIL items, then re-run"
    echo ""
    echo "  To fix: paste each ❌ FAIL item to the agent as specific feedback."
    echo "  Example: 'Fix: No Dockerfile in services/edge-agent — create a"
    echo "  Dockerfile using python:3.11-slim base with GStreamer packages.'"
else
    echo "  🔴 RESULT: SIGNIFICANT ISSUES — ${FAIL} failures need fixing"
    echo ""
    echo "  Consider: is the agent on the right track, or should you restart"
    echo "  with a clearer prompt? If >50% of checks fail, restart is faster."
fi
echo "═══════════════════════════════════════════════════════"
