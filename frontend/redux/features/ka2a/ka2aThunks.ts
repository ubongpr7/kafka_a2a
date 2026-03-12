import type { AppDispatch, RootState } from "@/redux/store"
import { streamEnded, streamErrored, streamStarted } from "./ka2aSlice"
import { ka2aApiSlice } from "./ka2aApiSlice"

const _errorMessage = (error: unknown): string => {
  if (error instanceof Error) return error.message
  if (error && typeof error === "object") {
    const record = error as { data?: unknown; error?: unknown }
    if (typeof record.error === "string") return record.error
    if (typeof record.data === "string") return record.data
    if (record.data && typeof record.data === "object") {
      const detail = (record.data as { detail?: unknown }).detail
      if (typeof detail === "string") return detail
    }
  }
  return String(error)
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
      const request = dispatch(
        ka2aApiSlice.endpoints.streamMessage.initiate({
          sessionId,
          text,
          agentName: session.agentName,
          contextId: session.contextId,
          historyLength: session.historyLength,
        }),
      )
      try {
        await request.unwrap()
      } finally {
        request.reset()
      }
    } catch (err: unknown) {
      dispatch(streamErrored({ sessionId, error: _errorMessage(err) }))
      return
    }

    dispatch(streamEnded({ sessionId }))
  }
