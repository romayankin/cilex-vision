#!/usr/bin/env bash
set -euo pipefail
echo "🔌 Running Connector integration checks..."
ERRORS=0

echo "📋 Check 1: Proto files lint..."
if [ -f proto/buf.yaml ]; then
    cd proto && buf lint 2>/dev/null && echo "  ✓ Proto lint passed" || { echo "  ❌ Proto lint failed"; ERRORS=$((ERRORS+1)); }
    cd ..
else
    echo "  ⏭  No buf.yaml yet — skipping"
fi

echo "📋 Check 2: Dockerfiles exist for all services..."
for svc in services/*/; do
    svc_name=$(basename "$svc")
    if [ -f "${svc}/main.py" ] && [ ! -f "${svc}/Dockerfile" ]; then
        echo "  ❌ ${svc_name} has main.py but no Dockerfile"
        ERRORS=$((ERRORS+1))
    fi
done

echo "📋 Check 3: docker-compose validates..."
if [ -f infra/docker-compose.yml ]; then
    docker-compose -f infra/docker-compose.yml config -q 2>/dev/null && echo "  ✓ docker-compose valid" || echo "  ⚠  docker-compose validation failed"
else
    echo "  ⏭  No docker-compose.yml yet"
fi

echo ""
if [ $ERRORS -gt 0 ]; then
    echo "❌ ${ERRORS} issues found."
    exit 1
else
    echo "✅ All connector checks passed."
fi
