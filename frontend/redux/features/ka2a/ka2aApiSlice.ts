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

const _requestJson = async <T>(path: string): Promise<{ data: T } | { error: FetchBaseQueryError }> => {
  try {
    const resp = await fetch(`${_getGatewayBaseUrl()}${path}`, {
      cache: "no-store",
    })
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
      queryFn: async () => _requestJson<GatewayHealth>("/health"),
    }),
    listAgents: builder.query<AgentCard[], void>({
      queryFn: async () => _requestJson<AgentCard[]>("/agents"),
    }),
    streamMessage: builder.mutation<void, StreamMessageArgs>({
      queryFn: async (args, api) => {
        try {
          const resp = await fetch(`${_getGatewayBaseUrl()}/stream`, {
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
          })

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
