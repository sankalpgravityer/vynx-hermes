#!/usr/bin/env bash
# Repair approved / verified products whose category does not match the product
# (reports/generated/category-mismatch-2026-10-28.xlsx), one tenant at a time,
# through the Auto Approval agent's own chain — no backend change, no new fixes.
#
# What the chain does for each product (scripts/repair_product.py):
#   refile     the AI reads the garment off the CUT-OUT photographs; when it
#              disagrees with the category the product is re-filed under the
#              tenant's matching subcategory (KLE-002772: heels filed as Jeans)
#   reconcile  category path, sizing guide and mannequin brought into line
#   copy       title and description rewritten for the corrected category
#   sync       the repaired product re-pushed to Shopify — ONLY when the tenant's
#              Brain switch "Sync repairs to live listings" is ON
# It never approves (no --approve), and products stay in their stage.
#
# Usage (from the Hermes repo root, on the Hermes server):
#   scripts/fix_category_mismatch.sh                    # DRY RUN, every tenant sheet
#   scripts/fix_category_mismatch.sh --apply            # write, every tenant sheet
#   scripts/fix_category_mismatch.sh --apply Klekt      # write, one tenant
#   scripts/fix_category_mismatch.sh --apply --workers 2 BOAS
#
# Before --apply:
#   * VNYX_API_URL points at the production vnyx-api (the preflight checks both
#     sides name the same database)
#   * the Brain switch "Sync repairs to live listings" is ON for each tenant
#   * no Celery agent run is working the same tenant (see run_from_sheet --workers)
set -euo pipefail

cd "$(dirname "$0")/.."
SHEETS_DIR="reports/generated/category-mismatch-fix"
APPLY=""
WORKERS=1
TENANTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY="--apply --yes" ;;
    --workers) WORKERS="$2"; shift ;;
    *) TENANTS+=("$1") ;;
  esac
  shift
done

[ -x .venv/bin/python ] || { echo "no .venv/bin/python — run from the Hermes repo root"; exit 1; }
[ -n "${VNYX_API_URL:-}" ] || { echo "VNYX_API_URL is not set — point it at the production vnyx-api"; exit 1; }
if [ ${#TENANTS[@]} -eq 0 ]; then
  for f in "$SHEETS_DIR"/*.xlsx; do TENANTS+=("$(basename "$f" .xlsx)"); done
fi

mkdir -p logs
STAMP=$(date +%Y%m%d-%H%M)
echo "Mode: ${APPLY:+APPLY}${APPLY:-DRY RUN} · workers per tenant: $WORKERS · tenants: ${TENANTS[*]}"
for t in "${TENANTS[@]}"; do
  sheet="$SHEETS_DIR/$t.xlsx"
  [ -f "$sheet" ] || { echo "no sheet for '$t' ($sheet) — skipped"; continue; }
  echo; echo "===== $t ($sheet) ====="
  # --include-approved: these products passed review already; repaired, never re-approved.
  # --retry: re-verify products the agent has judged before (a dry run leaves a verdict).
  # shellcheck disable=SC2086
  .venv/bin/python scripts/run_from_sheet.py --sheet "$sheet" --include-approved --retry \
    --workers "$WORKERS" --log "logs/category-fix-$t-$STAMP.log" $APPLY
done
echo; echo "Logs: logs/category-fix-*-$STAMP.log"
