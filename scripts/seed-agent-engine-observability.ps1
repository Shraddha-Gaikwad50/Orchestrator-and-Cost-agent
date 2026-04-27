param(
    [string]$CostAgentResource = "",
    [string]$EvalGcsDest = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

if (Test-Path "config/gcp.env") {
    $lines = Get-Content "config/gcp.env"
    foreach ($line in $lines) {
        if ($line -match '^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)\s*$') {
            $name = $matches[1]
            $value = $matches[2].Trim('"')
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

$project = $env:GOOGLE_CLOUD_PROJECT
$location = if ($env:GOOGLE_CLOUD_LOCATION) { $env:GOOGLE_CLOUD_LOCATION } else { "us-central1" }
$orchestratorResource = $env:ORCHESTRATOR_AGENT_ENGINE_RESOURCE
if (-not $CostAgentResource) { $CostAgentResource = $env:COST_AGENT_ENGINE_RESOURCE }
if (-not $EvalGcsDest) { $EvalGcsDest = $env:AGENT_ENGINE_EVAL_GCS_DEST }
$skipMemorySmoke = ($env:SKIP_MEMORY_SMOKE -eq "1")
$skipVertexPublish = ($env:SKIP_VERTEX_PUBLISH -eq "1")
$minimalVertexEval = ($env:MINIMAL_VERTEX_EVAL -eq "1")

if (-not $project) { throw "Set GOOGLE_CLOUD_PROJECT (e.g. in config/gcp.env)." }
if (-not $orchestratorResource) { throw "Set ORCHESTRATOR_AGENT_ENGINE_RESOURCE in config/gcp.env." }
if (-not $CostAgentResource) { throw "Set COST_AGENT_ENGINE_RESOURCE in config/gcp.env or pass -CostAgentResource." }
if ((-not $skipVertexPublish) -and (-not $EvalGcsDest)) { throw "Set AGENT_ENGINE_EVAL_GCS_DEST in config/gcp.env or pass -EvalGcsDest." }

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

if (-not $skipMemorySmoke) {
  & ".\.venv\Scripts\python.exe" "scripts/agent-engine-memory-smoke.py" `
    --project $project `
    --location $location `
    --resource $orchestratorResource `
    --resource $CostAgentResource `
    --scenarios "scripts/evals/memory_seed_cases.json" `
    --verify-memory `
    --memory-search-wait-seconds 30 `
    --memory-search-interval-seconds 5 `
    --out "logs/agent-engine-memory-seed-report-$timestamp.json"
}

$publishArgs = @()
if (-not $skipVertexPublish) {
  $publishArgs += @("--publish-to-vertex", "--gcs-dest", $EvalGcsDest)
  if ($minimalVertexEval) {
    $publishArgs += @("--minimal-vertex-eval")
  }
}

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-create-eval.py" `
  --project $project `
  --location $location `
  --resource $orchestratorResource `
  --cases "scripts/evals/agent_engine_eval_cases.json" `
  --fail-on-assertion `
  --min-pass-rate 0.90 `
  --fail-on-priority P0 `
  $publishArgs `
  --display-name "orchestrator-eval-single-$timestamp" `
  --label "component=orchestrator" `
  --label "suite=single_turn" `
  --label "dataset_version=v1" `
  --label "run_source=seed-agent-engine-observability" `
  --out "logs/agent-engine-eval-orchestrator-single-$timestamp.json"

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-create-eval.py" `
  --project $project `
  --location $location `
  --resource $orchestratorResource `
  --cases "scripts/evals/agent_engine_multiturn_cases.json" `
  --fail-on-assertion `
  --min-pass-rate 0.90 `
  --fail-on-priority P0 `
  $publishArgs `
  --display-name "orchestrator-eval-multiturn-$timestamp" `
  --label "component=orchestrator" `
  --label "suite=multi_turn" `
  --label "dataset_version=v1" `
  --label "run_source=seed-agent-engine-observability" `
  --out "logs/agent-engine-eval-orchestrator-multiturn-$timestamp.json"

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-create-eval.py" `
  --project $project `
  --location $location `
  --resource $CostAgentResource `
  --cases "scripts/evals/agent_engine_eval_cases.json" `
  --fail-on-assertion `
  --min-pass-rate 0.90 `
  --fail-on-priority P0 `
  $publishArgs `
  --display-name "cost-agent-eval-single-$timestamp" `
  --label "component=cost_agent" `
  --label "suite=single_turn" `
  --label "dataset_version=v1" `
  --label "run_source=seed-agent-engine-observability" `
  --out "logs/agent-engine-eval-cost-single-$timestamp.json"

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-create-eval.py" `
  --project $project `
  --location $location `
  --resource $CostAgentResource `
  --cases "scripts/evals/agent_engine_multiturn_cases.json" `
  --fail-on-assertion `
  --min-pass-rate 0.90 `
  --fail-on-priority P0 `
  $publishArgs `
  --display-name "cost-agent-eval-multiturn-$timestamp" `
  --label "component=cost_agent" `
  --label "suite=multi_turn" `
  --label "dataset_version=v1" `
  --label "run_source=seed-agent-engine-observability" `
  --out "logs/agent-engine-eval-cost-multiturn-$timestamp.json"

Write-Host "Observability seeding complete."
