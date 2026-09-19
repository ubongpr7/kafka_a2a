# JEV Decisioning: Host and Specialist Layer

> **Current reference:** [A2A JEV Decisioning Architecture](a2a-jev-decisioning-architecture.md)
> documents the complete host, specialist/tool, and LiveKit voice design. This
> historical filename is retained for rollout references; it now describes the
> host and specialist portion of the delivered implementation.

## Scope

The implementation adds an optional, platform-managed TypeSafe/JEV decisioning
layer for host-turn intent, specialist selection, LLM tool shortlisting,
specialist-result quality, and final-response quality. The companion LiveKit
runtime applies the same provider-neutral layer to voice-turn readiness,
clarification relation, and response speakability. Every capability starts in
shadow mode and records a bounded, redacted snapshot for comparison with the
existing system.

Specialist selection and the LLM-visible tool list can be enabled only through
an explicit enforcement allowlist and confidence threshold. The tool executor,
agent allowlists, permissions, confirmations, deterministic domain routines,
and write operations remain authoritative. A final-response retry is separately
gated, limited to one text-only/no-tool rewrite, and cannot repeat a tool,
delegation, mutation, or confirmation.

Tenant LLM configuration remains unchanged. The TypeSafe API key is a backend
platform secret and must not be placed in tenant settings, browser code, JWTs,
MCP configuration, prompts, or telemetry.

## Enablement

The feature is disabled by default. Enable it only after supplying the platform
secret and a pinned TypeSafe model ID. This environment currently uses the
account-supported `jev-latest` alias. Confirm the account's available aliases
with `GET /v1/models` before changing it:

```sh
KA2A_DECISIONING_ENABLED=true
KA2A_DECISIONING_PROVIDER=typesafe
KA2A_DECISIONING_MODE=shadow
TYPESAFE_API_KEY=...
KA2A_DECISIONING_MODEL=jev-latest
```

`KA2A_DECISIONING_API_KEY_ENV` may point to a differently named secret
environment variable. The remaining `KA2A_DECISIONING_*` controls are listed
in `.env.example`, including timeout, concurrency, sampling, retry, circuit
breaker, state-size, and policy-version settings.

For the complete implementation, `enforce` still requires an explicit
`KA2A_DECISIONING_ENFORCE_CAPABILITIES` allowlist. The host runtime permits
only `specialist_selection` and `tool_shortlist`; host intent and
specialist-result evaluation are telemetry-only. Final-response evaluation can
only trigger the separately gated text-only retry described above. The isolated
voice runtime has its own allowlist and can influence only turn readiness and
clarification handling; its speakability check is telemetry-only. This keeps
JEV outside authorization, confirmations, write actions, and tool loops.

## Provider Contract

The TypeSafe adapter is the only module coupled to the vendor. It calls the
documented direct HTTP API at `POST https://api.typesafe.ai/v1/systemone` with
the platform bearer token and a pinned model. The adapter validates the returned
choice, candidate probability distribution, confidence, and usage before it is
accepted by the provider-neutral decision service.

The decision service has a short timeout, at most one retry for transient
failures, bounded worker concurrency, and a circuit breaker. Disabled
configuration, missing credentials, timeouts, invalid provider results, and
service failures all fail open and leave host behavior untouched.

## Observability and Privacy

Structured `decisioning_shadow` events, and `decisioning_enforcement` events
when an explicit enforcement gate is evaluated, record workspace/profile attribution,
task and trace identifiers, candidate IDs, baseline intent, JEV choice,
probabilities, confidence, latency, retry count, outcome, fallback category,
and token usage when returned. Events contain a keyed hash of the submitted
state rather than the state itself.

The submitted state is limited to the current user message, a bounded text-only
history, and a small allow-list of workflow fields. It omits tool results,
agent prompts, credentials, bearer tokens, API keys, and raw request metadata.
The telemetry topic setting is included in structured events for future pipeline
wiring; the implementation intentionally relies on the existing runtime log
and metrics path rather than introducing a new durable Kafka schema.

## Rollout

1. Deploy with decisioning disabled and confirm normal A2A behavior.
2. Enable shadow mode at a low deterministic sample rate and exercise all five
   capabilities.
3. Compare JEV choices and confidence against baseline outcomes, latency,
   fallbacks, and cost.
4. Enable only one low-risk enforcement capability after review; keep the
   confidence threshold conservative.
5. Leave write operations, authorization, confirmations, and deterministic
   business workflows under existing A2A control.
