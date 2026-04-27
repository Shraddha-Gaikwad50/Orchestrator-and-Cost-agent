#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "config/gcp.env" ]]; then
  # shellcheck disable=SC1091
  source "config/gcp.env"
fi

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
ORCH_RESOURCE="${ORCHESTRATOR_AGENT_ENGINE_RESOURCE:-}"
COST_RESOURCE="${COST_AGENT_ENGINE_RESOURCE:-${1:-}}"
GCS_DEST="${AGENT_ENGINE_EVAL_GCS_DEST:-${2:-}}"
SKIP_MEMORY_SMOKE="${SKIP_MEMORY_SMOKE:-0}"
SKIP_VERTEX_PUBLISH="${SKIP_VERTEX_PUBLISH:-0}"
MINIMAL_VERTEX_EVAL="${MINIMAL_VERTEX_EVAL:-0}"

if [[ -z "$PROJECT" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT (e.g. in config/gcp.env)."
  exit 1
fi
if [[ -z "$ORCH_RESOURCE" ]]; then
  echo "Set ORCHESTRATOR_AGENT_ENGINE_RESOURCE in config/gcp.env."
  exit 1
fi
if [[ -z "$COST_RESOURCE" ]]; then
  echo "Set COST_AGENT_ENGINE_RESOURCE in config/gcp.env or pass it as first argument."
  exit 1
fi
if [[ "$SKIP_VERTEX_PUBLISH" != "1" && -z "$GCS_DEST" ]]; then
  echo "Set AGENT_ENGINE_EVAL_GCS_DEST in config/gcp.env or pass gs://... as second argument."
  exit 1
fi

TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
echo "Project: $PROJECT"
echo "Location: $LOCATION"
echo "Orchestrator resource: $ORCH_RESOURCE"
echo "Cost resource: $COST_RESOURCE"
echo "Eval GCS dest: ${GCS_DEST:-<not required when SKIP_VERTEX_PUBLISH=1>}"
echo "SKIP_MEMORY_SMOKE: $SKIP_MEMORY_SMOKE"
echo "SKIP_VERTEX_PUBLISH: $SKIP_VERTEX_PUBLISH"
echo "MINIMAL_VERTEX_EVAL: $MINIMAL_VERTEX_EVAL"

if [[ "$SKIP_MEMORY_SMOKE" != "1" ]]; then
  ./.venv/bin/python "scripts/agent-engine-memory-smoke.py" \
    --project "$PROJECT" \
    --location "$LOCATION" \
    --resource "$ORCH_RESOURCE" \
    --resource "$COST_RESOURCE" \
    --scenarios "scripts/evals/memory_seed_cases.json" \
    --verify-memory \
    --memory-search-wait-seconds 30 \
    --memory-search-interval-seconds 5 \
    --out "logs/agent-engine-memory-seed-report-$TIMESTAMP.json"
fi

PUBLISH_ARGS=()
if [[ "$SKIP_VERTEX_PUBLISH" != "1" ]]; then
  PUBLISH_ARGS+=(--publish-to-vertex --gcs-dest "$GCS_DEST")
  if [[ "$MINIMAL_VERTEX_EVAL" == "1" ]]; then
    PUBLISH_ARGS+=(--minimal-vertex-eval)
  fi
fi

./.venv/bin/python "scripts/agent-engine-create-eval.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$ORCH_RESOURCE" \
  --cases "scripts/evals/agent_engine_eval_cases.json" \
  --fail-on-assertion \
  --min-pass-rate 0.90 \
  --fail-on-priority P0 \
  "${PUBLISH_ARGS[@]}" \
  --display-name "orchestrator-eval-single-$TIMESTAMP" \
  --label "component=orchestrator" \
  --label "suite=single_turn" \
  --label "dataset_version=v1" \
  --label "run_source=seed-agent-engine-observability" \
  --out "logs/agent-engine-eval-orchestrator-single-$TIMESTAMP.json"

./.venv/bin/python "scripts/agent-engine-create-eval.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$ORCH_RESOURCE" \
  --cases "scripts/evals/agent_engine_multiturn_cases.json" \
  --fail-on-assertion \
  --min-pass-rate 0.90 \
  --fail-on-priority P0 \
  "${PUBLISH_ARGS[@]}" \
  --display-name "orchestrator-eval-multiturn-$TIMESTAMP" \
  --label "component=orchestrator" \
  --label "suite=multi_turn" \
  --label "dataset_version=v1" \
  --label "run_source=seed-agent-engine-observability" \
  --out "logs/agent-engine-eval-orchestrator-multiturn-$TIMESTAMP.json"

./.venv/bin/python "scripts/agent-engine-create-eval.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$COST_RESOURCE" \
  --cases "scripts/evals/agent_engine_eval_cases.json" \
  --fail-on-assertion \
  --min-pass-rate 0.90 \
  --fail-on-priority P0 \
  "${PUBLISH_ARGS[@]}" \
  --display-name "cost-agent-eval-single-$TIMESTAMP" \
  --label "component=cost_agent" \
  --label "suite=single_turn" \
  --label "dataset_version=v1" \
  --label "run_source=seed-agent-engine-observability" \
  --out "logs/agent-engine-eval-cost-single-$TIMESTAMP.json"

./.venv/bin/python "scripts/agent-engine-create-eval.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$COST_RESOURCE" \
  --cases "scripts/evals/agent_engine_multiturn_cases.json" \
  --fail-on-assertion \
  --min-pass-rate 0.90 \
  --fail-on-priority P0 \
  "${PUBLISH_ARGS[@]}" \
  --display-name "cost-agent-eval-multiturn-$TIMESTAMP" \
  --label "component=cost_agent" \
  --label "suite=multi_turn" \
  --label "dataset_version=v1" \
  --label "run_source=seed-agent-engine-observability" \
  --out "logs/agent-engine-eval-cost-multiturn-$TIMESTAMP.json"

echo "Observability seeding complete."
