import type { FetchBaseQueryError } from "@reduxjs/toolkit/query"

import { apiSlice } from "../../services/apiSlice"
import { eventReceived, type Ka2aEvent } from "./ka2aSlice"

type GatewayHealth = {
  status: string
}

type AgentCard = Record<string, unknown>

type StreamMessageArgs = {
  sessionId: string
  text: string
  agentName: string
  contextId?: string
  historyLength: number
}

const _stripTrailingSlash = (value: string) => value.replace(/\/+$/, "")

const _getGatewayBaseUrl = () => {
  const base = (process.env.NEXT_PUBLIC_KA2A_GATEWAY_URL || "http://localhost:8000").trim()
  return _stripTrailingSlash(base)
}

const _getRequestTimeoutMs = () => {
  const raw = (process.env.NEXT_PUBLIC_KA2A_REQUEST_TIMEOUT_MS || "").trim()
  if (!raw) return 0
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed <= 0) return 0
  return Math.floor(parsed)
}

const _fetchError = (error: unknown): { error: FetchBaseQueryError } => ({
  error: {
    status: "FETCH_ERROR",
    error: error instanceof Error ? error.message : String(error),
  },
})

const _httpError = (status: number, data: unknown): { error: FetchBaseQueryError } => ({
  error: { status, data },
})

const _parseJson = <T>(text: string): T => JSON.parse(text) as T

const _fetchWithOptionalTimeout = async (
  input: RequestInfo | URL,
  init: RequestInit = {},
  externalSignal?: AbortSignal,
) => {
  const timeoutMs = _getRequestTimeoutMs()
  if (!externalSignal && timeoutMs <= 0) {
    return fetch(input, init)
  }

  const controller = new AbortController()
  const cleanup: Array<() => void> = []

  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort(externalSignal.reason)
    } else {
      const abortFromExternal = () => controller.abort(externalSignal.reason)
      externalSignal.addEventListener("abort", abortFromExternal, { once: true })
      cleanup.push(() => externalSignal.removeEventListener("abort", abortFromExternal))
    }
  }

  let timer: ReturnType<typeof setTimeout> | undefined
  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      controller.abort(new Error(`Request timed out after ${timeoutMs}ms`))
    }, timeoutMs)
  }

  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } finally {
    if (timer !== undefined) clearTimeout(timer)
    cleanup.forEach((fn) => fn())
  }
}

const _requestJson = async <T>(
  path: string,
  signal?: AbortSignal,
): Promise<{ data: T } | { error: FetchBaseQueryError }> => {
  try {
    const resp = await _fetchWithOptionalTimeout(`${_getGatewayBaseUrl()}${path}`, {
      cache: "no-store",
    }, signal)
    const text = await resp.text()

    if (!resp.ok) {
      return _httpError(resp.status, text || resp.statusText)
    }
    if (!text.trim()) {
      return { data: {} as T }
    }
    return { data: _parseJson<T>(text) }
  } catch (error) {
    return _fetchError(error)
  }
}

const _parseSseChunks = async (
  stream: ReadableStream<Uint8Array>,
  onJson: (obj: unknown) => void,
): Promise<void> => {
  const reader = stream.getReader()
  const decoder = new TextDecoder("utf-8")
  let buffer = ""

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    while (true) {
      const sepIndex = buffer.indexOf("\n\n")
      if (sepIndex === -1) break
      const rawEvent = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)

      const lines = rawEvent.split(/\r?\n/)
      const dataLines: string[] = []
      for (const line of lines) {
        if (line.startsWith("data:")) {
          dataLines.push(line.slice("data:".length).trimStart())
        }
      }
      if (!dataLines.length) continue

      const dataText = dataLines.join("\n").trim()
      if (!dataText) continue
      try {
        onJson(JSON.parse(dataText) as unknown)
      } catch {
        // Ignore malformed chunks and keep streaming.
      }
    }
  }
}

export const ka2aApiSlice = apiSlice.injectEndpoints({
  endpoints: (builder) => ({
    getGatewayHealth: builder.query<GatewayHealth, void>({
      queryFn: async (_arg, api) => _requestJson<GatewayHealth>("/health", api.signal),
    }),
    listAgents: builder.query<AgentCard[], void>({
      queryFn: async (_arg, api) => _requestJson<AgentCard[]>("/agents", api.signal),
    }),
    streamMessage: builder.mutation<void, StreamMessageArgs>({
      queryFn: async (args, api) => {
        try {
          const resp = await _fetchWithOptionalTimeout(`${_getGatewayBaseUrl()}/stream`, {
            method: "POST",
            headers: {
              "content-type": "application/json",
            },
            body: JSON.stringify({
              text: args.text,
              agentName: args.agentName,
              contextId: args.contextId,
              historyLength: args.historyLength,
            }),
          }, api.signal)

          if (!resp.ok) {
            const detail = await resp.text().catch(() => "")
            return _httpError(resp.status, detail || resp.statusText)
          }
          if (!resp.body) {
            return _fetchError("No response body")
          }

          await _parseSseChunks(resp.body, (obj) => {
            if (!obj || typeof obj !== "object" || Array.isArray(obj)) return
            const record = obj as Record<string, unknown>
            if (typeof record.kind !== "string") return
            api.dispatch(eventReceived({ sessionId: args.sessionId, event: record as Ka2aEvent }))
          })

          return { data: undefined }
        } catch (error) {
          return _fetchError(error)
        }
      },
    }),
  }),
})

export const {
  useGetGatewayHealthQuery,
  useListAgentsQuery,
} = ka2aApiSlice
