import { getGatewayBaseUrl } from "../_gateway"

export const runtime = "nodejs"

export async function POST(request: Request) {
  const base = getGatewayBaseUrl()
  // Force all user messages through the host router agent so it can delegate.
  // This avoids accidentally talking directly to a downstream agent from the UI.
  let body: unknown = null
  try {
    body = await request.json()
  } catch {
    body = null
  }

  const payload: Record<string, unknown> =
    body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : {}

  // Never allow overriding the router identity from the client.
  payload.agentName = "host"

  const upstream = await fetch(`${base}/stream`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  })

  // Preserve streaming body.
  const headers = new Headers()
  headers.set("content-type", "text/event-stream")
  headers.set("cache-control", "no-cache, no-transform")

  return new Response(upstream.body, {
    status: upstream.status,
    headers,
  })
}
