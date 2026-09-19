# Kafka A2A and JEV Decisioning Architecture

**Status:** Implemented platform decisioning for host, specialist/tool, response-quality, and LiveKit voice-turn policy. Voice enforcement is disabled by default and must be explicitly enabled after shadow review.

**Audience:** Intera IMS product, platform, operations, and engineering teams.

## 1. Purpose and Boundaries

JEV is a platform-owned decisioning dependency. It makes bounded, typed choices from a fixed candidate list; it is not a tenant-selected LLM, a user-facing chat model, an agent, or an authority for permissions and business writes.

Tenants continue to configure their own conversational LLM provider and model for their workspace. The platform holds `TYPESAFE_API_KEY` only in backend service environments. The key is never sent to the browser, a tenant setting, a JWT, an MCP request, an agent prompt, or decision telemetry.

JEV may improve selection or evaluate quality. Existing deterministic policy remains authoritative for authorization, agent allowlists, confirmation gates, write operations, task protocol handling, and safe fallbacks.

## 2. Request Flow

```text
Text widget or LiveKit transcript
  -> gateway / voice safety and transcript handling
  -> host agent (conversation state, clarification, planning)
  -> specialist agent(s) selected from authorized candidates
  -> LLM-visible MCP tool shortlist
  -> MCP service / read or confirmed write workflow
  -> specialist artifact(s) and structured widgets
  -> host synthesis, final response evaluation, streamed result
```

JEV runs alongside this path. In shadow mode it records a comparison without changing the baseline. In enforce mode it can select only from explicitly allowed, pre-authorized alternatives that pass the configured confidence threshold.

## 3. Host and Orchestrator Layer

The host owns the conversation, stateful follow-up resolution, targeted clarification, multi-domain planning, delegation, and final synthesis. It is the only component that turns a voice or text request into a cross-specialist plan.

### Implemented host decisions

| Capability | Baseline owner | JEV role | Enforcement boundary |
|---|---|---|---|
| Host-turn intent | Host conversation policy | Classifies new request, follow-up, clarification answer, acknowledgement, broad review, and global import request | Shadow only; host workflow remains authoritative |
| Specialist selection | Host routing policy | Chooses only among already authorized specialists | Explicit allowlist and confidence threshold |
| Tool shortlist | Specialist/host tool policy | Reduces only the tools visible to the LLM | Direct deterministic tools, permissions, and executor remain unchanged |
| Specialist result quality | Specialist terminal result | Evaluates whether the result answers the request | Shadow only |
| Final response quality | Host synthesized result | Evaluates answer quality; may permit one text-only rewrite | Explicit allowlist, high confidence, no tool/delegation/write retry |

Broad business reviews are deliberately protected from reordering: the host keeps its approved multi-domain plan while JEV records the decision for comparison.

## 4. Specialist and MCP Tool Layer

Specialists receive only the work delegated by the host and operate through their existing authorization, tenant scoping, and MCP tool contracts. JEV does not call MCP services and never chooses an unregistered tool or agent.

For a tool decision, the host first builds an authorized candidate set. JEV may then return exactly one listed tool or `no_tool_needed`. If JEV is unavailable, malformed, slow, low-confidence, rate-limited, or disabled, the full baseline tool list is retained. Deterministic workflows and all mutation/confirmation paths are intentionally outside JEV control.

## 5. LiveKit Voice Layer

The LiveKit worker preserves the existing safety-first sequence:

1. Final STT turns are buffered and deduplicated after a short silence.
2. Known greetings, repeat/status requests, explicit cancellations, and transcription corrections are handled locally.
3. Existing rules merge compatible fragments, preserve pending clarification, and retain one A2A context for the call.
4. A complete business request is sent only to the host agent, never directly to a specialist or tool.
5. Host stream events are rendered to the UI and filtered into safe, concise voice updates.

### Implemented voice JEV capabilities

| Capability | Decision candidates | How it is applied |
|---|---|---|
| Voice-turn readiness | Wait, delegate to host, ask time range, ask metric scope, ask clarification, ignore | Only after deterministic cancellation/local-command checks. Candidate set is restricted by the known request state. |
| Voice-turn relation | New request, clarification answer, continue clarification | Helps distinguish topic switching from an answer to the active clarification. A semantic choice cannot bypass structural merge validation. |
| Voice-response speakability | Speak, suppress, needs concise summary | Shadow evaluation of whether a selected host result is appropriate to say aloud. Existing sanitization and summary selection remain authoritative. |

Voice JEV does not process audio directly. It receives only bounded, redacted text state after STT. It does not decide cancellation, interruption, identity, permissions, confirmations, specialist routing, tool routing, or writes.

Each LiveKit session builds a session-local decision runtime. This is required because the local voice worker executes jobs in threads and an async HTTP client or decision queue must not cross event loops. Session shutdown drains queued shadow telemetry for a bounded interval, then closes the provider safely.

## 6. Configuration and Rollout

Required platform configuration for host shadow mode:

```env
KA2A_DECISIONING_ENABLED=true
KA2A_DECISIONING_PROVIDER=typesafe
KA2A_DECISIONING_MODE=shadow
KA2A_DECISIONING_MODEL=jev-latest
TYPESAFE_API_KEY=platform-managed-secret
```

Voice remains off unless explicitly enabled:

```env
KA2A_VOICE_DECISIONING_ENABLED=true
KA2A_VOICE_DECISIONING_TIMEOUT_S=0.25
KA2A_VOICE_DECISIONING_SHADOW_SAMPLE_RATE=0.10
KA2A_VOICE_DECISIONING_ENFORCE_MIN_CONFIDENCE=0.92
KA2A_VOICE_DECISIONING_ENFORCE_CAPABILITIES=
KA2A_VOICE_DECISIONING_DRAIN_TIMEOUT_S=0.25
```

Recommended rollout:

1. Run host and voice in shadow mode and compare baseline versus JEV decisions.
2. Review divergence, fallback rate, latency, token use, and false delegation/clarification outcomes.
3. Enable only `voice_turn_readiness` first, with global mode `enforce`, the voice allowlist, and confidence at least `0.92`.
4. Keep response speakability in shadow until voice transcripts demonstrate that it improves user experience without delaying results.
5. Never enable JEV for authorization, confirmation, write execution, or direct MCP calls.

## 7. Failure, Privacy, and Observability

The decision service uses bounded queues, concurrency limits, timeout, circuit breaker, and fail-open behavior. Host uses its configured decision timeout and retry policy; voice uses a separate short timeout with no retry. A timeout, network error, invalid decision, queue-full condition, low confidence, or circuit-open condition immediately returns the existing behavior.

Structured `decisioning_shadow` and `decisioning_enforcement` events include profile/workspace attribution, task or voice-turn ID, context ID, agent surface, candidate IDs, baseline decision, JEV choice, probability distribution, confidence, latency, retry count, error/fallback category, model, and token usage. They retain a keyed state hash rather than raw decision state.

Bounded decision state excludes credentials, bearer tokens, authorization headers, passwords, API keys, raw MCP payloads, tool results, audio, and raw internal stream metadata.

## 8. Code Map

| Area | Primary modules |
|---|---|
| Provider-neutral contracts/configuration | `src/kafka_a2a/decisioning/contracts.py`, `config.py`, `factory.py` |
| TypeSafe adapter and safe dispatch | `provider_typesafe.py`, `service.py`, `telemetry.py` |
| Host/specialist/tool integration | `facade.py`, `langgraph_processor.py`, `runtime/shared_runtime.py` |
| LiveKit voice integration | `livekit_voice/worker.py` |
| Regression tests | `tests/test_decisioning.py`, `tests/test_livekit_voice_worker.py` |
| Deployment configuration | `.env.example`, Docker compose files with the `decisioning` extra |

## 9. Verification Evidence

Focused static checks passed for the decisioning package, LiveKit worker, and related tests. The focused JEV plus voice suite passed 87 tests. The suite includes platform-key handling, provider response validation, state redaction, sampling, circuit failure, specialist/tool enforcement, final-response gating, voice runtime configuration, high/low confidence voice action behavior, transcript readiness, clarification relation, cancellation, fragment merge, and Naira speech handling.

Manual acceptance remains necessary for actual microphone quality, device output selection, mute controls, LiveKit connectivity, and TypeSafe billing/account limits.
