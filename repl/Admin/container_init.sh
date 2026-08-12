#!/usr/bin/env bash
#
# DESCRIPTION: container entrypoint — ensure Isabelle components are registered
# in the (possibly stale) isabelle_user_data volume, then exec the container
# command. Fixes docs/ISSUES.md Bug 7: after an image rebuild the named volume
# shadows the image's /root/.isabelle, so component registration done at build
# time is lost ("Not found: py4j").
#
# Gated for speed: repl/Admin/init is idempotent (existing contribs are
# skipped), but we only run it when registration is actually missing.

set -euo pipefail

cd /app

ISABELLE="${ISABELLE_HOME:-/opt/isabelle}/bin/isabelle"
ISABELLE_IDENTIFIER="$(ISABELLE_COMPONENTS='' "$ISABELLE" getenv -b ISABELLE_IDENTIFIER 2>/dev/null || echo Isabelle2025-2)"
USER_HOME="${HOME}/.isabelle/${ISABELLE_IDENTIFIER}"
COMPONENTS_FILE="${USER_HOME}/etc/components"

if [[ -f "$COMPONENTS_FILE" ]] \
    && grep -qx "/app/repl" "$COMPONENTS_FILE" \
    && grep -q "contrib/py4j" "$COMPONENTS_FILE" \
    && grep -q "contrib/spliff" "$COMPONENTS_FILE"; then
  echo "container_init: components already registered in volume, skipping init"
else
  echo "container_init: component registration missing in volume — running repl/Admin/init"
  ./repl/Admin/init
fi

exec "$@"
