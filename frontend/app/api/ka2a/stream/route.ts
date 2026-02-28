import { getGatewayBaseUrl } from "../_gateway"

export const runtime = "nodejs"

export async function POST(request: Request) {
  const base = getGatewayBaseUrl()
  const bodyText = await request.text()

  const upstream = await fetch(`${base}/stream`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: bodyText,
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

