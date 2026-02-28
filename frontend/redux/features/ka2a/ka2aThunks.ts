import type { AppDispatch, RootState } from "@/redux/store"
import { eventReceived, streamEnded, streamErrored, streamStarted, type Ka2aEvent } from "./ka2aSlice"

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

    // SSE events are separated by a blank line.
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
        // Ignore malformed chunks; keep streaming.
      }
    }
  }
}

export const sendStreamMessage =
  (args: { text: string }) => async (dispatch: AppDispatch, getState: () => RootState) => {
    const state = getState()
    const sessionId = state.ka2a.activeSessionId
    if (!sessionId) return
    const session = state.ka2a.sessions[sessionId]
    if (!session) return

    const text = args.text.trim()
    if (!text) return

    dispatch(streamStarted({ sessionId, userText: text }))

    try {
      const body = {
        text,
        agentName: session.agentName,
        contextId: session.contextId,
        historyLength: session.historyLength,
      }
      const resp = await fetch("/api/ka2a/stream", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      })
      if (!resp.ok) {
        const detail = await resp.text().catch(() => "")
        throw new Error(detail || `HTTP ${resp.status}`)
      }
      if (!resp.body) {
        throw new Error("No response body")
      }

      await _parseSseChunks(resp.body, (obj) => {
        if (!obj || typeof obj !== "object" || Array.isArray(obj)) return
        const record = obj as Record<string, unknown>
        if (typeof record.kind !== "string") return
        dispatch(eventReceived({ sessionId, event: record as unknown as Ka2aEvent }))
      })
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err)
      dispatch(streamErrored({ sessionId, error: message }))
      return
    }

    dispatch(streamEnded({ sessionId }))
  }
