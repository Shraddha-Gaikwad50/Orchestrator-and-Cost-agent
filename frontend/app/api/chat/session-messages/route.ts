import { getOrchestratorAuthHeaders } from "@/lib/orchestrator-server-auth";
import { ORCHESTRATOR_UPSTREAM } from "@/lib/orchestrator-upstream";

/**
 * Proxies GET /chat/sessions/{id}/messages without a nested dynamic segment
 * (Next build had trouble resolving /api/chat/sessions/[sessionId]/messages).
 */
export async function GET(req: Request) {
  const sessionId = new URL(req.url).searchParams.get("sessionId")?.trim();
  if (!sessionId) {
    return new Response(JSON.stringify({ error: "sessionId query required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const auth = await getOrchestratorAuthHeaders();
  const upstream = await fetch(
    `${ORCHESTRATOR_UPSTREAM}/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...auth,
      },
    }
  );

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "Content-Type":
        upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
