import { getGatewayBaseUrl } from "../_gateway"

export const runtime = "nodejs"

export async function GET() {
  const base = getGatewayBaseUrl()
  const upstream = await fetch(`${base}/agents`, { cache: "no-store" })
  const text = await upstream.text()
  return new Response(text, { status: upstream.status, headers: { "content-type": "application/json" } })
}

