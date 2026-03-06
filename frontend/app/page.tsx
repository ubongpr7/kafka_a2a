"use client"

import * as React from "react"

import { useAppDispatch, useAppSelector } from "@/redux/hooks"
import {
  clearSession,
  createSession,
  deleteSession,
  setActiveSession,
  updateSessionConfig,
} from "@/redux/features/ka2a/ka2aSlice"
import { sendStreamMessage } from "@/redux/features/ka2a/ka2aThunks"
import { ThemeSwitchButton } from "@/redux/theme-switch-button"

const platformName = process.env.NEXT_PUBLIC_PLATFORM_NAME || "K-A2A Playground"

const _asObj = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

const _asStr = (value: unknown): string => (typeof value === "string" ? value : "")

const _fmtTime = (iso?: string) => {
  if (!iso) return ""
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}

export default function HomePage() {
  const dispatch = useAppDispatch()
  const { sessions, activeSessionId } = useAppSelector((s) => s.ka2a)

  const session = activeSessionId ? sessions[activeSessionId] : undefined
  const [text, setText] = React.useState("")
  const [gatewayOk, setGatewayOk] = React.useState<boolean | null>(null)

  React.useEffect(() => {
    if (!activeSessionId) {
      dispatch(createSession())
    }
  }, [activeSessionId, dispatch])

  React.useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const resp = await fetch("/api/ka2a/health", { cache: "no-store" })
        if (cancelled) return
        setGatewayOk(resp.ok)
      } catch {
        if (cancelled) return
        setGatewayOk(false)
      }
    }
    run()
    const t = setInterval(run, 5000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  const onSend = async () => {
    if (!session) return
    const value = text.trim()
    if (!value || session.isStreaming) return
    setText("")
    await dispatch(sendStreamMessage({ text: value }))
  }

  const onKeyDown: React.KeyboardEventHandler<HTMLTextAreaElement> = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      void onSend()
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <p className="eyebrow">Kafka A2A Playground</p>
          <h1 className="title">{platformName}</h1>
          <p className="subtitle">
            Gateway:{" "}
            <span className={`badge ${gatewayOk === false ? "bad" : gatewayOk === true ? "ok" : ""}`}>
              {gatewayOk === null ? "checking…" : gatewayOk ? "online" : "offline"}
            </span>
          </p>
        </div>
        <div className="actions">
          <ThemeSwitchButton />
        </div>
      </header>

      <div className="grid">
        <section className="panel chat">
          <div className="panel-header">
            <div className="panel-title">Chat</div>
            <div className="panel-meta">
              <span className="meta">
                Agent: <code>{session?.agentName || "—"}</code>
              </span>
              <span className="meta">
                Context: <code className="mono">{session?.contextId || "new"}</code>
              </span>
              <span className="meta">
                History: <code>{session?.historyLength ?? 0}</code>
              </span>
            </div>
          </div>

          <div className="messages">
            {!session?.messages.length ? (
              <div className="empty">
                <p>Send a message to start a session. Reuse the same contextId to keep memory.</p>
              </div>
            ) : null}
            {session?.messages.map((m) => (
              <div key={m.id} className={`bubble ${m.role}`}>
                <div className="bubble-meta">
                  <span className="role">{m.role}</span>
                  <span className="time">{_fmtTime(m.timestamp)}</span>
                  {m.taskId ? <span className="task">task {m.taskId.slice(0, 8)}…</span> : null}
                </div>
                <div className="bubble-content">{m.content}</div>
              </div>
            ))}
          </div>

          <div className="composer">
            {session?.error ? <div className="error">Error: {session.error}</div> : null}
            <textarea
              className="input"
              placeholder={session?.isStreaming ? "Waiting for the agent…" : "Type your message (Enter to send)"}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKeyDown}
              rows={3}
              disabled={!session || session.isStreaming}
            />
            <div className="composer-actions">
              <button className="btn secondary" onClick={() => session && dispatch(clearSession({ sessionId: session.sessionId }))} disabled={!session || session.isStreaming}>
                Clear UI
              </button>
              <button className="btn" onClick={onSend} disabled={!session || session.isStreaming || !text.trim()}>
                {session?.isStreaming ? "Streaming…" : "Send"}
              </button>
            </div>
          </div>
        </section>

        <section className="panel trace">
          <div className="panel-header">
            <div className="panel-title">Session</div>
            <div className="panel-actions">
              <button className="btn secondary" onClick={() => dispatch(createSession())}>
                New
              </button>
              {session ? (
                <button className="btn danger" onClick={() => dispatch(deleteSession(session.sessionId))} disabled={session.isStreaming}>
                  Delete
                </button>
              ) : null}
            </div>
          </div>

          <div className="section">
            <div className="field">
              <label>Active Session</label>
              <select
                className="select"
                value={activeSessionId || ""}
                onChange={(e) => dispatch(setActiveSession(e.target.value))}
              >
                {Object.values(sessions).map((s) => (
                  <option key={s.sessionId} value={s.sessionId}>
                    {s.sessionId.slice(0, 8)}… {s.contextId ? `(${s.contextId.slice(0, 8)}…)` : ""}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label>Agent Name</label>
              <input
                className="text"
                value={session?.agentName || ""}
                placeholder="host"
                disabled
              />
              <div className="hint">All UI requests go to <code>host</code>; it delegates to other agents.</div>
            </div>

            <div className="field">
              <label>History Length</label>
              <input
                className="text"
                type="number"
                value={session?.historyLength ?? 0}
                onChange={(e) =>
                  session &&
                  dispatch(
                    updateSessionConfig({
                      sessionId: session.sessionId,
                      historyLength: Number(e.target.value),
                    }),
                  )
                }
                min={0}
                max={50}
                disabled={!session || session.isStreaming}
              />
              <div className="hint">How many prior turns to inject into the LLM prompt (0–50).</div>
            </div>

            <div className="field">
              <label>Context ID</label>
              <div className="mono-block">
                <code className="mono">{session?.contextId || "— (created after first message)"}</code>
                {session?.contextId ? (
                  <button
                    className="btn tiny secondary"
                    onClick={() => navigator.clipboard.writeText(session.contextId || "")}
                    type="button"
                  >
                    Copy
                  </button>
                ) : null}
              </div>
            </div>
          </div>

          <div className="panel-header">
            <div className="panel-title">Events</div>
            <div className="panel-meta">
              <span className="meta">
                Last task: <code>{session?.lastTaskId ? `${session.lastTaskId.slice(0, 8)}…` : "—"}</code>
              </span>
            </div>
          </div>

          <div className="events">
            {(session?.eventLog || []).slice().reverse().map((item) => {
              const e = _asObj(item.event)
              const kind = _asStr(e.kind) || "event"
              const status = _asObj(e.status)
              const state = kind === "status-update" ? _asStr(status.state) : ""
              const ts =
                kind === "status-update"
                  ? _asStr(status.timestamp)
                  : kind === "task"
                    ? _asStr(status.timestamp)
                    : ""
              const label = state ? `${kind} (${state})` : kind
              return (
                <details key={item.id} className="event">
                  <summary>
                    <span className="event-kind">{label}</span>
                    <span className="event-time">{_fmtTime(ts || item.receivedAt)}</span>
                  </summary>
                  <pre className="json">{JSON.stringify(item.event, null, 2)}</pre>
                </details>
              )
            })}
          </div>
        </section>
      </div>
    </main>
  )
}
