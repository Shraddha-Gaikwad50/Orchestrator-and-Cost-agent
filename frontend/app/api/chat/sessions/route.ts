import { getOrchestratorAuthHeaders } from "@/lib/orchestrator-server-auth";
import { ORCHESTRATOR_UPSTREAM } from "@/lib/orchestrator-upstream";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const qs = url.searchParams.toString();
  const path = qs
    ? `${ORCHESTRATOR_UPSTREAM}/chat/sessions?${qs}`
    : `${ORCHESTRATOR_UPSTREAM}/chat/sessions`;

  const auth = await getOrchestratorAuthHeaders();
  const upstream = await fetch(path, {
    method: "GET",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...auth,
    },
  });

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "Content-Type":
        upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
