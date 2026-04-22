# Frontend (Next.js 14)

This UI streams assistant responses from the orchestrator SSE endpoint.

## Environment

For local development, the UI always talks to the local orchestrator bridge at
`http://127.0.0.1:8000`. It does **not** call Vertex directly from the browser.

- If you use the full-stack launcher (`scripts/start-all.sh` on macOS/Linux),
  it auto-generates `frontend/.env.development.local` with the correct values.
- If you run frontend manually, create `frontend/.env.local` with:

```bash
NEXT_PUBLIC_ORCHESTRATOR_URL=http://127.0.0.1:8000
NEXT_PUBLIC_USE_CHAT_PROXY=1
ORCHESTRATOR_SERVER_URL=http://127.0.0.1:8000
```
The bridge emits typed cost payloads for rendering (`response_type=clarification|result|error`), so clarification prompts are kept as plain text and not rendered as result tables.

### Invoking Vertex AI Agent Engine on every message

To have each chat go through your deployed **`pa_orchestrator_agent`** (so GCP metrics and sessions update), configure the **Python orchestrator** (`agents/orchestrator`), not the frontend URL:

1. Authenticate for local ADC: `gcloud auth application-default login`
2. Set `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` (e.g. `us-central1`)
3. Set **`ORCHESTRATOR_AGENT_ENGINE_RESOURCE`** to the full resource from the console (**Copy identity**), e.g.  
   `projects/your-project/locations/us-central1/reasoningEngines/8296018091465244672`  
   Alternatively set **`ORCHESTRATOR_AGENT_ENGINE_QUERY_URL`** to the **Copy query URL** value.
4. Reinstall orchestrator deps (`google-cloud-aiplatform` is in `agents/orchestrator/requirements.txt`) and restart uvicorn on port 8000.

When this is active, `GET http://127.0.0.1:8000/health` includes `"agent_engine_chat_enabled": true` and each message uses `stream_query` on that engine.

See `vertex_agents/AGENT-ENGINE-DEPLOY.txt` and `config/gcp.env.example`.

## Local Run

From `frontend/`:

```bash
npm ci
npm run dev
```

Open `http://127.0.0.1:3000`.

## Full Local Stack

From repo root:

- macOS/Linux:

```bash
bash scripts/start-all.sh
```

- Windows PowerShell:

```powershell
.\scripts\start-all.ps1
```

This starts:
- Postgres on `5435` (host; compose mapping)
- Orchestrator on `8000`
- Frontend on `3000`

Note: there is no local `cost-agent` service in this architecture anymore. Cost logic runs in the deployed `cost_metrics_agent` via the deployed `pa_orchestrator_agent` that the local orchestrator bridge calls.

## Agent Engine Observability (Memories + Evaluation)

From repo root, use the one-shot helper after deploys to populate observability tabs:

- macOS/Linux: `scripts/seed-agent-engine-observability.sh`
- Windows: `scripts/seed-agent-engine-observability.ps1`

Required env vars:

- `ORCHESTRATOR_AGENT_ENGINE_RESOURCE`
- `COST_AGENT_ENGINE_RESOURCE`
- `AGENT_ENGINE_EVAL_GCS_DEST`

Stop services:

- macOS/Linux:

```bash
pkill -f 'uvicorn main:app'
pkill -f 'next dev'
docker compose down
```

- Windows PowerShell:

```powershell
.\scripts\stop-all.ps1
docker compose down
```
