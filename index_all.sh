#!/usr/bin/env bash
# =============================================================================
# index_all.sh — Bulk semantic indexing for OpenArchiver RAG
#
# Usage:
#   ./index_all.sh [TOTAL_EMAILS] [BATCH_SIZE] [SLEEP_SECONDS] [API_URL]
#
# Examples:
#   ./index_all.sh                          # auto-detect total from API
#   ./index_all.sh 233364                   # known total, default batch 1000
#   ./index_all.sh 233364 500 120           # total=233364, batch=500, sleep=120s
#   ./index_all.sh 233364 1000 180 http://localhost:8001
#
# Run in background (survives SSH disconnect):
#   nohup ./index_all.sh 233364 >> indexing.log 2>&1 &
#   tail -f indexing.log
#
# Check progress in the web UI at http://YOUR-NAS-IP:8090
# =============================================================================

set -euo pipefail

# ── Arguments (all optional) ──────────────────────────────────────────────────
API_URL="${4:-http://localhost:8001}"
BATCH="${2:-1000}"
SLEEP_SEC="${3:-180}"

# Auto-detect total from API if not provided
if [ -z "${1:-}" ]; then
  echo "[$(date '+%H:%M:%S')] Auto-detecting total email count from API..."
  TOTAL=$(curl -s "${API_URL}/index/status" | grep -o '"postgres_total":[0-9]*' | grep -o '[0-9]*')
  if [ -z "$TOTAL" ]; then
    echo "ERROR: Could not reach ${API_URL}/index/status — is the backend running?"
    exit 1
  fi
  echo "[$(date '+%H:%M:%S')] Detected ${TOTAL} emails in database."
else
  TOTAL="${1}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
BATCHES=$(( (TOTAL + BATCH - 1) / BATCH ))
EST_MINUTES=$(( BATCHES * SLEEP_SEC / 60 ))

echo "============================================================"
echo " OpenArchiver RAG — Bulk Indexing"
echo "============================================================"
echo " API URL     : ${API_URL}"
echo " Total emails: ${TOTAL}"
echo " Batch size  : ${BATCH}"
echo " Sleep       : ${SLEEP_SEC}s between batches"
echo " Batches     : ~${BATCHES}"
echo " Est. runtime: ~${EST_MINUTES} minutes"
echo "============================================================"
echo " Already-indexed emails are automatically skipped."
echo " Safe to re-run at any time."
echo "============================================================"
echo ""

# ── Loop ──────────────────────────────────────────────────────────────────────
OFFSET=0
BATCH_NUM=0

while [ "$OFFSET" -lt "$TOTAL" ]; do
  BATCH_NUM=$(( BATCH_NUM + 1 ))
  TIMESTAMP="[$(date '+%Y-%m-%d %H:%M:%S')]"

  echo "${TIMESTAMP} Batch ${BATCH_NUM}/${BATCHES} — offset=${OFFSET}, limit=${BATCH}"

  RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${API_URL}/index" \
    -H "Content-Type: application/json" \
    -d "{\"limit\":${BATCH},\"offset\":${OFFSET}}")

  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  BODY=$(echo "$RESPONSE" | head -1)

  if [ "$HTTP_CODE" != "200" ]; then
    echo "${TIMESTAMP} ERROR: HTTP ${HTTP_CODE} — ${BODY}"
    echo "${TIMESTAMP} Retrying in 60 seconds..."
    sleep 60
    continue
  fi

  echo "${TIMESTAMP} Response: ${BODY}"

  OFFSET=$(( OFFSET + BATCH ))

  # Don't sleep after the last batch
  if [ "$OFFSET" -lt "$TOTAL" ]; then
    echo "${TIMESTAMP} Waiting ${SLEEP_SEC}s for batch to complete..."
    sleep "$SLEEP_SEC"
  fi
done

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] All batches submitted."
echo "Check indexing progress: curl -s ${API_URL}/index/status | python3 -m json.tool"
