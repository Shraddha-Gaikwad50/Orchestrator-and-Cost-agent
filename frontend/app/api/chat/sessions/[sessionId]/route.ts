import { getOrchestratorAuthHeaders } from "@/lib/orchestrator-server-auth";
import { ORCHESTRATOR_UPSTREAM } from "@/lib/orchestrator-upstream";

type Params = { params: { sessionId: string } };

export async function DELETE(_req: Request, { params }: Params) {
  const { sessionId } = params;
  const auth = await getOrchestratorAuthHeaders();
  const upstream = await fetch(
    `${ORCHESTRATOR_UPSTREAM}/chat/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "DELETE",
      headers: { ...auth },
    }
  );

  if (upstream.status === 204) {
    return new Response(null, { status: 204 });
  }

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "Content-Type":
        upstream.headers.get("content-type") ?? "text/plain; charset=utf-8",
    },
  });
}
