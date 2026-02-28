const _stripTrailingSlash = (value: string) => value.replace(/\/+$/, "")

export const getGatewayBaseUrl = () => {
  const fromEnv = process.env.KA2A_GATEWAY_URL || process.env.NEXT_PUBLIC_KA2A_GATEWAY_URL
  const base = (fromEnv || "http://localhost:8000").trim()
  return _stripTrailingSlash(base)
}

