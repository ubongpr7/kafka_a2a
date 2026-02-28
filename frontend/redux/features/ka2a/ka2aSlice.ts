import { createSlice, type PayloadAction } from "@reduxjs/toolkit"

type Ka2aRole = "user" | "assistant"

export type Ka2aEventKind = "task" | "status-update" | "artifact-update"

export type Ka2aEvent = {
  kind: Ka2aEventKind
  [key: string]: unknown
}

export type ChatMessage = {
  id: string
  role: Ka2aRole
  content: string
  taskId?: string
  timestamp: string
}

export type EventLogItem = {
  id: string
  receivedAt: string
  event: Ka2aEvent
}

export type TaskRun = {
  taskId: string
  contextId?: string
  state?: string
  submittedAt?: string
  completedAt?: string
  resultText?: string
}

export type Ka2aSession = {
  sessionId: string
  title: string
  agentName: string
  contextId?: string
  historyLength: number
  isStreaming: boolean
  lastTaskId?: string
  messages: ChatMessage[]
  eventLog: EventLogItem[]
  runs: Record<string, TaskRun>
  error?: string
}

export type Ka2aState = {
  sessions: Record<string, Ka2aSession>
  activeSessionId?: string
}

const _id = () => {
  const cryptoAny = globalThis.crypto as unknown as { randomUUID?: () => string } | undefined
  if (cryptoAny?.randomUUID) return cryptoAny.randomUUID()
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const _nowIso = () => new Date().toISOString()

const _asObj = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

const _asArr = (value: unknown): unknown[] => (Array.isArray(value) ? value : [])

const _asStr = (value: unknown): string => (typeof value === "string" ? value : "")

const _asBool = (value: unknown): boolean => (typeof value === "boolean" ? value : Boolean(value))

const _asId = (value: unknown): string => {
  if (typeof value === "string") return value
  if (typeof value === "number") return String(value)
  return ""
}

const _newSession = (): Ka2aSession => ({
  sessionId: _id(),
  title: "New session",
  agentName: "host",
  historyLength: 10,
  isStreaming: false,
  messages: [],
  eventLog: [],
  runs: {},
})

const initialState: Ka2aState = {
  sessions: {},
  activeSessionId: undefined,
}

const ka2aSlice = createSlice({
  name: "ka2a",
  initialState,
  reducers: {
    createSession: (state) => {
      const session = _newSession()
      state.sessions[session.sessionId] = session
      state.activeSessionId = session.sessionId
    },
    setActiveSession: (state, action: PayloadAction<string>) => {
      if (state.sessions[action.payload]) {
        state.activeSessionId = action.payload
      }
    },
    deleteSession: (state, action: PayloadAction<string>) => {
      delete state.sessions[action.payload]
      if (state.activeSessionId === action.payload) {
        const next = Object.keys(state.sessions)[0]
        state.activeSessionId = next
      }
    },
    updateSessionConfig: (
      state,
      action: PayloadAction<{ sessionId: string; agentName?: string; historyLength?: number }>,
    ) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return
      if (typeof action.payload.agentName === "string") session.agentName = action.payload.agentName
      if (typeof action.payload.historyLength === "number")
        session.historyLength = Math.max(0, Math.floor(action.payload.historyLength))
    },
    setSessionContextId: (state, action: PayloadAction<{ sessionId: string; contextId?: string }>) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return
      session.contextId = action.payload.contextId
    },
    streamStarted: (state, action: PayloadAction<{ sessionId: string; userText: string }>) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return
      session.isStreaming = true
      session.error = undefined
      session.messages.push({
        id: _id(),
        role: "user",
        content: action.payload.userText,
        timestamp: _nowIso(),
      })
    },
    streamEnded: (state, action: PayloadAction<{ sessionId: string }>) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return
      session.isStreaming = false
    },
    streamErrored: (state, action: PayloadAction<{ sessionId: string; error: string }>) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return
      session.isStreaming = false
      session.error = action.payload.error
    },
    eventReceived: (state, action: PayloadAction<{ sessionId: string; event: Ka2aEvent }>) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return

      const receivedAt = _nowIso()
      session.eventLog.push({ id: _id(), receivedAt, event: action.payload.event })

      const event = action.payload.event
      if (event.kind === "task") {
        const taskId = _asId(event.id)
        const contextId = _asStr(event.contextId) || undefined
        session.contextId = contextId || session.contextId
        session.lastTaskId = taskId || session.lastTaskId

        const status = _asObj(event.status)
        const stateValue = _asStr(status.state) || undefined
        const submittedAt = _asStr(status.timestamp) || undefined

        if (taskId) {
          session.runs[taskId] = {
            taskId,
            contextId,
            state: stateValue,
            submittedAt,
          }
        }
        return
      }

      if (event.kind === "status-update") {
        const taskId = _asId(event.taskId)
        const contextId = _asStr(event.contextId) || undefined
        if (contextId) session.contextId = contextId

        const status = _asObj(event.status)
        const stateValue = _asStr(status.state) || undefined
        const ts = _asStr(status.timestamp) || undefined
        const final = _asBool(event.final)

        if (taskId) {
          const run = session.runs[taskId] || { taskId }
          run.contextId = run.contextId || contextId
          run.state = stateValue || run.state
          if (final) run.completedAt = ts || run.completedAt
          session.runs[taskId] = run
          session.lastTaskId = taskId
        }

        if (final) {
          session.isStreaming = false
        }
        return
      }

      if (event.kind === "artifact-update") {
        const taskId = _asId(event.taskId)
        const contextId = _asStr(event.contextId) || undefined
        if (contextId) session.contextId = contextId

        const artifact = _asObj(event.artifact)
        const artifactName = _asStr(artifact.name)

        if (artifactName === "result") {
          const parts = _asArr(artifact.parts)
          const text = parts
            .map((p) => _asObj(p))
            .filter((p) => _asStr(p.kind) === "text" && typeof p.text === "string")
            .map((p) => String(p.text))
            .join("")
            .trim()

          if (text) {
            session.messages.push({
              id: _id(),
              role: "assistant",
              content: text,
              taskId: taskId || undefined,
              timestamp: _nowIso(),
            })
          }

          if (taskId) {
            const run = session.runs[taskId] || { taskId }
            run.contextId = run.contextId || contextId
            run.resultText = text || run.resultText
            session.runs[taskId] = run
            session.lastTaskId = taskId
          }
        }
      }
    },
    clearSession: (state, action: PayloadAction<{ sessionId: string }>) => {
      const session = state.sessions[action.payload.sessionId]
      if (!session) return
      session.messages = []
      session.eventLog = []
      session.runs = {}
      session.error = undefined
      session.isStreaming = false
      // keep contextId so you can continue the session after clearing the UI
    },
  },
})

export const {
  createSession,
  setActiveSession,
  deleteSession,
  updateSessionConfig,
  setSessionContextId,
  streamStarted,
  streamEnded,
  streamErrored,
  eventReceived,
  clearSession,
} = ka2aSlice.actions

export default ka2aSlice.reducer
