#!/usr/bin/env bash
# Signalpost one-command run.
#
#   bash run.sh --organisations smoke-companies.jsonl --bulk brreg-enheter.csv \
#     --run-id smoke-001 --expected-count 100
#
# Pipeline:
#   1. Baseline batch (registry, financials, roles, group, locations, website)
#      via scripts/run_competition_batch.py — the evaluator-owned contract.
#   2. Free external connectors (no API keys, $0 third-party cost):
#      Google News RSS, Fagfolkguiden reviews, LinkedIn profile discovery,
#      LinkedIn guest jobs (only for verified handles), YouTube channel search.
#      Brave Search discovery runs ONLY when BRAVE_SEARCH_API_KEY is set in
#      the environment (free tier needs a key); otherwise that module is
#      recorded as not_applicable, never as a failure.
#   3. Merge + deterministic synthesis (templates only, no LLM) via
#      scripts/merge_external_observations.py, which re-emits terminal
#      envelopes and validates them.
#
# A connector that fails never drops an envelope: its module is recorded
# as source_error / not_found / not_applicable and the run continues.
# Exit code is 0 only when the merged envelopes validate
# (exactly --expected-count envelopes, one per input, zero silent drops).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

ORGANISATIONS=""
BULK=""
RUN_ID=""
EXPECTED_COUNT=100
OUT_DIR=""
SKIP_LINKEDIN_JOBS=0
SKIP_YOUTUBE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --organisations) ORGANISATIONS="$2"; shift 2 ;;
    --bulk) BULK="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --expected-count) EXPECTED_COUNT="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --skip-linkedin-jobs) SKIP_LINKEDIN_JOBS=1; shift ;;
    --skip-youtube) SKIP_YOUTUBE=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$ORGANISATIONS" ] || { echo "--organisations is required" >&2; exit 2; }
[ -n "$BULK" ] || { echo "--bulk is required" >&2; exit 2; }
[ -n "$RUN_ID" ] || { echo "--run-id is required" >&2; exit 2; }
[ -z "$OUT_DIR" ] && OUT_DIR="out/$RUN_ID"
mkdir -p "$OUT_DIR" "$OUT_DIR/cache/fagfolk" "$OUT_DIR/cache/linkedin" "$OUT_DIR/cache/linkedin-jobs"

STARTED_AT=$(date -u +%s)
log() { echo "[$(date -u +%H:%M:%S)] $*" >&2; }

log "Step 0: clean install (uv sync --frozen)"
uv sync --frozen --quiet

log "Step 1: baseline batch ($EXPECTED_COUNT companies)"
uv run python scripts/run_competition_batch.py \
  --organisations "$ORGANISATIONS" \
  --bulk "$BULK" \
  --profiles-output "$OUT_DIR/profiles.jsonl" \
  --output "$OUT_DIR/envelopes.jsonl" \
  --report "$OUT_DIR/batch-report.json" \
  --run-id "$RUN_ID" \
  --expected-count "$EXPECTED_COUNT"

python3 - "$ORGANISATIONS" "$OUT_DIR/orgs.txt" <<'PYEOF'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
orgs = []
for line in open(src, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
        orgs.append(str(row["organisation_number"]))
    except Exception:
        orgs.append(line)
open(dst, "w", encoding="utf-8").write("\n".join(orgs) + "\n")
print(f"wrote {len(orgs)} organisation numbers")
PYEOF

# Observation files always exist (empty when a stage is skipped/failed).
for name in news reviews linkedin-handles linkedin-jobs youtube; do
  : > "$OUT_DIR/obs-$name.jsonl"
done

log "Step 2: Google News RSS (exact-title, last 2 years)"
if ! uv run python scripts/run_google_news_rss_connector.py \
  --profiles "$OUT_DIR/profiles.jsonl" \
  --organisations "$OUT_DIR/orgs.txt" \
  --output "$OUT_DIR/obs-news.jsonl" \
  --report "$OUT_DIR/report-news.json" \
  --per-company 5 --years 2 --workers 4; then
  log "WARNING: news connector failed; module will be recorded as source_error"
fi

log "Step 3: Fagfolkguiden reviews (embedded Google aggregate rating)"
if ! uv run python scripts/run_fagfolkguiden_reviews_connector.py \
  --profiles "$OUT_DIR/profiles.jsonl" \
  --organisations "$OUT_DIR/orgs.txt" \
  --output "$OUT_DIR/obs-reviews.jsonl" \
  --cache "$OUT_DIR/cache/fagfolk" \
  --report "$OUT_DIR/report-reviews.json" \
  --workers 4; then
  log "WARNING: reviews connector failed; module will be recorded as source_error"
fi

log "Step 4: LinkedIn company profile discovery (typeahead + domain/location gates)"
if ! uv run python scripts/discover_linkedin_company_profiles.py \
  --profiles "$OUT_DIR/profiles.jsonl" \
  --output "$OUT_DIR/obs-linkedin-handles.jsonl" \
  --report "$OUT_DIR/report-linkedin-discovery.json" \
  --cache-dir "$OUT_DIR/cache/linkedin" \
  --workers 2 --delay 2.0 --timeout 20; then
  log "WARNING: LinkedIn discovery failed; module will be recorded as source_error"
fi

if [ "$SKIP_LINKEDIN_JOBS" -eq 0 ]; then
  log "Step 5: LinkedIn guest jobs (verified handles only, 1 page)"
  if ! uv run python scripts/run_linkedin_guest_jobs_connector.py \
    --profiles "$OUT_DIR/profiles.jsonl" \
    --handles "$OUT_DIR/obs-linkedin-handles.jsonl" \
    --organisations "$OUT_DIR/orgs.txt" \
    --output "$OUT_DIR/obs-linkedin-jobs.jsonl" \
    --report "$OUT_DIR/report-linkedin-jobs.json" \
    --cache-dir "$OUT_DIR/cache/linkedin-jobs" \
    --pages 1 --delay 1.0 --timeout 20; then
    log "WARNING: LinkedIn jobs connector failed; module will be recorded as source_error"
  fi
else
  log "Step 5: LinkedIn jobs skipped (--skip-linkedin-jobs)"
fi

if [ "$SKIP_YOUTUBE" -eq 0 ]; then
  log "Step 6: YouTube exact-name channel search"
  if ! uv run python scripts/run_youtube_search_connector.py \
    --profiles "$OUT_DIR/profiles.jsonl" \
    --handles "$OUT_DIR/obs-linkedin-handles.jsonl" \
    --organisations "$OUT_DIR/orgs.txt" \
    --output "$OUT_DIR/obs-youtube.jsonl" \
    --report "$OUT_DIR/report-youtube.json" \
    --limit "$EXPECTED_COUNT" --search-results 8; then
    log "WARNING: YouTube connector failed; module will be recorded as source_error"
  fi
else
  log "Step 6: YouTube skipped (--skip-youtube)"
fi

BRAVE_ARGS=()
if [ -n "${BRAVE_SEARCH_API_KEY:-}" ]; then
  log "Step 7: Brave Search discovery (key configured, transient results only)"
  if uv run python scripts/run_brave_discovery.py \
    --input "$OUT_DIR/profiles.jsonl" \
    --output "$OUT_DIR/profiles-brave.jsonl" \
    --report "$OUT_DIR/report-brave.json" \
    --limit "$EXPECTED_COUNT" --count 10 --promote-verified; then
    BRAVE_ARGS=(--brave-profiles "$OUT_DIR/profiles-brave.jsonl" --brave-report "$OUT_DIR/report-brave.json")
  else
    log "WARNING: Brave discovery failed; module will be recorded as source_error"
    BRAVE_ARGS=(--brave-report "$OUT_DIR/report-brave.json")
  fi
else
  log "Step 7: Brave discovery skipped (no BRAVE_SEARCH_API_KEY; module not_applicable)"
fi

log "Step 8: merge observations + deterministic synthesis + validate"
uv run python scripts/merge_external_observations.py \
  --profiles "$OUT_DIR/profiles.jsonl" \
  --envelopes "$OUT_DIR/envelopes.jsonl" \
  --news "$OUT_DIR/obs-news.jsonl" \
  --reviews "$OUT_DIR/obs-reviews.jsonl" \
  --linkedin-handles "$OUT_DIR/obs-linkedin-handles.jsonl" \
  --linkedin-jobs "$OUT_DIR/obs-linkedin-jobs.jsonl" \
  --youtube "$OUT_DIR/obs-youtube.jsonl" \
  --news-report "$OUT_DIR/report-news.json" \
  --reviews-report "$OUT_DIR/report-reviews.json" \
  --linkedin-discovery-report "$OUT_DIR/report-linkedin-discovery.json" \
  --linkedin-jobs-report "$OUT_DIR/report-linkedin-jobs.json" \
  --youtube-report "$OUT_DIR/report-youtube.json" \
  "${BRAVE_ARGS[@]}" \
  --output "$OUT_DIR/final-envelopes.jsonl" \
  --profiles-output "$OUT_DIR/final-profiles.jsonl" \
  --report "$OUT_DIR/run-report.json" \
  --run-id "$RUN_ID" \
  --expected-count "$EXPECTED_COUNT"

ENDED_AT=$(date -u +%s)
log "Done in $((ENDED_AT - STARTED_AT))s. Final envelopes: $OUT_DIR/final-envelopes.jsonl"
log "Final profiles: $OUT_DIR/final-profiles.jsonl  |  Run report: $OUT_DIR/run-report.json"
