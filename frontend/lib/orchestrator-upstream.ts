/** Server-only: base URL for orchestrator Route Handler proxies. */
export const ORCHESTRATOR_UPSTREAM =
  process.env.ORCHESTRATOR_SERVER_URL ?? "http://127.0.0.1:8000";
