# A2A Backend Capability Gap Audit

**Audit date:** 2026-10-09  
**Scope:** Kafka A2A host and specialist orchestration, LangGraph processor, conversation state, MCP execution, POS analytics contracts, JEV decisioning, gateway/runtime behavior, and LiveKit-to-host handoff.  
**Method:** static backend review, targeted deterministic reproduction of the reported follow-up, inspection of the configured local decisioning mode, and review of existing automated coverage. This is not a live production-data or browser acceptance run.

## Hardening Progress

### Autonomous Hardening Update

The following changes were implemented after this audit rather than left as
recommendations:

1. **Canonical analytic evidence now travels with every structured insight.**
   `analytics_facts.py` creates a versioned, presentation-independent fact
   packet from the values already rendered to the user. It carries dimensions,
   entity labels and IDs when available, metrics, values, units, currency,
   source widgets, coverage, data sources, and time-window metadata. The
   packet is attached at the A2A output boundary and is also persisted in the
   tenant-scoped context-memory record.
2. **Saved-report ranking answers are evidence-first.** A later question such
   as `What is the location with the lowest sales?` is answered from matching
   location-revenue facts before any legacy widget parsing or specialist
   routing. The answer retains the analysis period and, for a minimum result,
   can state the gap to the leading location. A product `top sellers` subset
   can answer a highest query but is deliberately rejected for lowest/zero
   claims.
3. **Topic-switch containment is covered at the host boundary.** A new product
   import request after a sales report goes straight to onboarding and cannot
   be answered from the old report. The real reported wording, `What about the
   amount of products I have sold for the past two years?`, now takes the POS
   total-units path rather than the Product Catalog specialist.
4. **Structured evidence quality is observable.** Every emitted insight with a
   fact packet includes `ka2a_fact_validation` metadata. This reports the
   schema version, numeric fact count, and whether usable canonical evidence
   was present; it does not expose raw MCP data.
5. **Read MCP calls have bounded recovery.** Configured MCP reads whose remote
   tool names start with `get_`, `list_`, `search_`, `find_`, `lookup_`,
   `count_`, or `summarize_` retry only transient failures. Writes are never
   retried automatically. A direct configured route now re-raises its actual
   failure instead of hiding it behind a second dynamic lookup and an
   `Unknown tool` error.
6. **JEV observes structured results without reading raw tool payloads.** A
   final `DataPart` insight now contributes only its user-facing title,
   summary, and explanation to the shadow evaluator. The evaluator continues
   to leave the typed payload unchanged and cannot trigger a retry of a
   tool-backed answer.
7. **Timeout and CORS defaults match the deployment contract.** Direct gateway
   startup now defaults streamed A2A calls to the same 900-second budget used
   by Compose; an explicit non-positive environment value still disables the
   gateway stream timeout. The proxy's default loopback CORS regular expression
   also correctly accepts `127.0.0.1` development origins.
8. **Conversation evidence is durably available to active sessions.** Local,
   server, and ECS development configuration explicitly select Redis-backed
   context memory with a seven-day TTL. This keeps tenant-scoped validated fact
   packets and pending clarifications available across browser reconnects and
   voice/text handoffs while bounding retention.
9. **Task and chat history use the same durable runtime dependency.** Local
   and ECS-development A2A processes now select the Redis task store rather
   than per-process memory. This preserves A2A task state, event replay, and
   server-owned conversation history when a worker is restarted.
10. **Structured follow-up context now has a server-owned source of truth.**
    When a completed A2A task contains a structured result artifact, follow-up
    routing reconstructs that payload from the durable task store instead of
    trusting a browser-supplied widget snapshot. Direct structured history is
    retained only as a compatibility fallback for legacy text-only tasks.
11. **Multi-domain review coverage is explicit.** Each business-review source
    check now reports complete, partial, or unavailable coverage. The host
    carries that status into each domain section and into the final review
    summary, so a timeout or malformed MCP result cannot be counted as a
    completed domain while valid sibling widgets remain available.
12. **Resolved workflow state cannot be resurrected by a memory write.** The
    context-memory write boundary reloads the current tenant-scoped record
    before merging a final analysis. A turn that completes a time-range
    clarification therefore preserves the cleared workflow state rather than
    restoring the stale state captured at the beginning of that turn.
13. **Canonical ranking follow-ups now have a deterministic contract.** The
    fact-packet path builds a bounded analytic frame and can inherit an omitted
    entity and metric only where the prior query or evidence makes them
    unambiguous. It validates that the selected rows really satisfy minimum,
    maximum, or zero-ranking semantics before composing text. Ambiguous broad
    reviews deliberately fall through to clarification or a fresh query rather
    than guessing from rendered widgets.
14. **Structured response contract results are emitted at the output boundary.**
    Every insight response now receives `ka2a_response_validation` metadata;
    attached fact packets also retain `ka2a_fact_validation`. Contradictory
    review coverage and malformed evidence are visible to operators and JEV
    observation without silently rewriting the user-facing answer.
15. **Contradictory review completion is corrected before delivery.** If a
    structured review claims complete coverage while its available-source count
    is lower than its attempted-source count, the output boundary downgrades
    it to partial, lowers confidence, and adds a user-facing warning.
16. **Voice playback and buffered-turn failures are no longer silent.** TTS
    provider exceptions are contained, and an unexpected buffered transcript
    processing failure publishes and speaks a concise retry response instead of
    terminating the background task without feedback.
17. **Explicit workspace timezones now control deterministic date windows.**
   Insight requests resolve a validated IANA timezone from trusted workspace
   metadata or principal claims, with UTC as the safe fallback. The selected
   timezone is included in the returned timeframe metadata so a tenant-local
   `today` boundary is reproducible without changing MCP argument contracts.
18. **Elliptical saved-result confirmations stay in the current conversation.**
   Phrases such as `to the analysis you just did` now resolve against the
   previous structured insight and return a concise confirmation instead of
   entering generic specialist routing. This closes the exact failure found in
   the exported conversation regression.

### Verification After Hardening

- `uv run pytest`: **479 passed**.
- The exact saved-result confirmation regression is covered and passes within
  the complete 479-test suite.
- `uv run ruff check` on all modified A2A source and test files: passed.
- `uv run python -m compileall -q src/kafka_a2a`: passed.
- `git diff --check`: passed.

Repository-wide Ruff still reports two pre-existing unused imports in
`mainapps/common/auth.py` and `utilities/mcp/extra_collab_tools.py`. They are
outside this hardening change and were intentionally not modified.

### Deployment Configuration Hardening

The runtime configuration now uses the same service contract in local Compose,
server Compose, and the generated ECS development parameters:

1. **Browser origins and required headers are explicitly allow-listed.** The
   local stack admits the supported Intera IMS and Hosperator deployment
   origins in addition to local development origins. Both local and server
   configurations allow the Intera authorization-context and frontend-origin
   headers used by the browser stream client. This prevents a successful
   backend route from being hidden behind a failed CORS preflight.
   The gateway's `/agent_api/management/agent-setup/` preflight is also covered
   for both `https://dev.interaims.com` and `https://dev.hosperator.com`.
   A CORS failure from the deployed `agents.dev.interaims.com` endpoint after
   this change therefore requires verification of the deployed container and
   edge proxy configuration; it is not evidence that the checked-in gateway
   policy omits either origin.
2. **Long-running analytical streams have a coherent request budget.**
   Gateway, proxy, and voice-to-host request budgets are set to 900 seconds.
   This preserves streaming status for multi-agent analysis rather than
   presenting a client-side timeout while a valid plan is still running.
3. **JEV remains platform-owned.** The ECS A2A service now declares the
   platform `TYPESAFE_API_KEY` plus all core and voice decisioning controls.
   No tenant credential is introduced. Voice decisioning is enabled in shadow
   mode with a short bounded decision timeout, so an unavailable decision
   provider cannot hold the LiveKit turn open.
4. **Deployment inputs are complete but not applied by this audit.** The ECS
   development parameter file includes the Redis task-store and context-memory
   controls and was regenerated successfully; Terraform validation passed.
   Applying infrastructure and starting containers remain explicit deployment
   actions; this document does not claim that a remote environment was changed.

Configuration validation after this change:

- Local and server Compose render successfully with the supported origins,
  request budgets, voice-decisioning fields, Redis task store, and bounded
  Redis context-memory fields present.
- `pytest tests/test_decisioning.py tests/test_livekit_voice_worker.py
  tests/test_fastapi_openapi.py`: **98 passed**.
- `infra/ecs-platform/scripts/validate_local.sh`: passed Terraform formatting,
  Python syntax, and Terraform validation.
- Live preflight probe on 2026-10-09: both `https://dev.interaims.com` and
  `https://dev.hosperator.com` requests to `https://agents.dev.interaims.com/stream`
  timed out after 10 seconds before receiving HTTP headers. This is recorded as
  deployment availability failure, not as a CORS-policy pass or failure.
- Read-only AWS verification on 2026-10-09 found zero ECS clusters and no
  load balancers in the configured `us-east-2` account, while DNS still
  resolves `agents.dev.interaims.com` and `a2a.dev.interaims.com` to the same
  edge address. No running A2A task was therefore available for live repair or
  acceptance testing.

Do not use a fully rendered Compose configuration as a shared diagnostic
artifact: environment interpolation can include deployment secrets. Validate
field presence without publishing resolved values.

### Required Live Acceptance Matrix

Automated tests prove deterministic host behavior with controlled data. The
following scenarios must be exercised against the running stack, using one
workspace with known seeded POS, inventory, procurement, and global-catalog
data. Capture the A2A task ID and gateway/runtime logs for every failure.

| ID | Channel | User sequence | Required result |
| --- | --- | --- | --- |
| `A2A-01` | Text | `Analyze my sales data.` then `past one year` | One targeted time-range clarification, then a POS-backed structured result for the resolved window. |
| `A2A-02` | Text | After `A2A-01`, ask `What is the location with the lowest sales?` | Answer from the same validated location-sales evidence; it must not state the top location as the lowest or call an unrelated specialist. |
| `A2A-03` | Text | After a sales answer, ask `What about the amount of products I have sold for the past two years?` | POS total-units flow, not Product Catalog; the response uses the requested two-year window. |
| `A2A-04` | Text | After any sales analysis, ask `I want to import products from the global catalog.` | New-topic routing to onboarding/global catalog. It must not be treated as a sales follow-up. |
| `A2A-05` | Text | `Analyze my business performance in the last year.` | A planned multi-domain review with per-domain structured artifacts, explicit completed/unavailable domains, and a final summary. |
| `A2A-06` | Text | Trigger a temporarily unavailable read MCP operation. | At most the configured bounded read retry; transparent partial/unavailable outcome; no automatic write retry. |
| `A2A-07` | Browser | Run `A2A-01` through `A2A-05`. | Each typed insight is rendered once; raw `TextPart(...)`/JSON representations are absent; distinct domain widgets stay distinct; PDF export has no raw JSON. |
| `A2A-08` | Browser origins | Send an authenticated stream request from the supported localhost and deployment origins. | Successful CORS preflight and stream response; unsupported origins are rejected. |
| `A2A-09` | Voice | Greeting, incomplete phrase, sales request with time-range clarification, then an unrelated import request. | Natural greeting; final utterances are merged before handoff; incomplete speech receives clarification; the host maintains the same semantic behavior as text; unrelated request switches topic. |
| `A2A-10` | Voice | Interrupt an in-progress utterance with a cancellation such as `no, don't do that`. | The pending destructive/operational continuation is cancelled or explicitly confirmed; it is never silently executed. |

The stack is not running in the current workspace, so these are deliberately
recorded as required acceptance tests rather than marked as passed.

### Completed In This Iteration

1. **Location-sales follow-ups are evidence-grounded.** The host now recognizes natural minimum and maximum variants such as `What is the location with the lowest sales?`, `Which branch had the least revenue?`, and `Which store performed the worst by turnover?`. It selects only from explicit location-sales widget rows, preserves the inherited review period, and includes the leading-location gap only as supporting context. Missing or malformed location facts fall through to normal routing rather than being treated as zero.
2. **Inventory-location questions cannot reuse a sales ranking.** A question such as `Which location has the lowest stock?` now exits the saved-sales path so the inventory specialist can provide current inventory evidence.
3. **Lowest-seller capability is now explicit.** The POS MCP `get_top_sellers` operation accepts `sort_order=highest|lowest`, and the POS specialist has a `bottom_sellers_seven_days` route. Lowest-seller widgets state that they rank products with completed POS sales only; they do not imply that unsold catalog products were checked.
4. **Saved top-seller widgets decline unsupported questions.** A truncated descending list no longer answers `Which product had the lowest sales?` or `Which product costs the least?`. Those questions return to normal specialist routing instead of receiving an invented answer.
5. **Calendar-bound orchestration tests are deterministic.** Business-review tests now freeze the processor clock, so their expected prior-quarter ranges do not fail merely because the real date changes.

### Verification

- `uv run pytest tests/test_langgraph_processor.py`: 165 passed.
- `uv run pytest tests/test_livekit_voice_worker.py tests/test_agent_filter.py`: 77 passed.
- `uv run python manage.py test --keepdb mainapps.point_of_sale.tests_mcp.PosMcpSalesAnalyticsTests`: 2 passed.
- `uv run ruff check src/kafka_a2a/langgraph_processor.py tests/test_langgraph_processor.py`: passed.

### Remaining Before Calling The Service Production-Ready

The highest-risk follow-up and MCP recovery gaps are now contained in code.
The following are still required for a production launch claim:

1. **Live acceptance against the running stack.** Run the text, browser,
   voice, cross-origin, and seeded-data scenarios below against the actual
   services. This cannot be substituted by unit tests while the stack is
   stopped.
2. **Server-owned durable conversation plans.** Redis-backed tenant-scoped
   context memory now preserves validated fact packets and pending
   clarifications across active sessions, while completed structured A2A task
   artifacts are used ahead of browser-supplied history. Task history, active
   plans, and pending interactions still need one durable append-only
   conversation record to become a single authoritative state model.
3. **Cross-domain specialist contracts.** The host now reports source
   availability for every completed business-review section, but the required
   data set for each domain is still best-effort rather than a versioned
   per-domain contract. Define required/optional sources and minimum evidence
   rules before treating a review as a certified management report.
4. **Authoritative location identity.** Tenant-local business-day windows now
   honor explicit workspace timezone metadata, but durable structural-location
   IDs are not yet enforced end to end across every specialist and historical
   POS record.
5. **End-to-end execution traces and production telemetry storage.** JEV and
   MCP logs are useful, but a durable correlated execution trace is still
   needed for support and quality dashboards.

## Executive Assessment

The service has many strong point solutions: typed A2A parts, tenant-aware MCP context, specialist agents, structured insight widgets, clarification workflows, an optional decisioning layer, and a meaningful regression suite. It is not yet a reliably general conversational analytics system.

The main problem is architectural rather than a missing sales tool. A user request is represented as text and then repeatedly reinterpreted by separate heuristic layers. The backend does not preserve a canonical semantic request such as:

```text
domain=pos
entity=location
metric=sales_revenue
operation=minimum
time_window=last_three_weeks
scope=workspace
relation=follow_up_to_sales_review
```

As a result, a previous widget can be read as an informal source of truth, and a phrase that is not one of the exact supported wordings can escape the follow-up handler. The remaining generic route is then free to choose a new interpretation or produce a high-sales answer. That is consistent with the reported result: asking for the location with the *lowest* sales and receiving the location with the *highest* sales.

The immediate priority is to make analytics follow-ups semantic, evidence-grounded, and directionally validated. More agents or more MCP tools will otherwise expand the number of ways the system can be wrong.

## Verified Reproduction Before Hardening

Before the first hardening change, using `_latest_insight_follow_up_answer` with a valid saved location-contribution insight containing `Maitama: 9,000` and `Agric: 2,500`:

| Follow-up | Current result |
| --- | --- |
| `What is the location with the lowest sales?` | `None` - no direct answer is generated from the saved insight. |
| `Which location has the lowest revenue?` | Correctly returns `Agric`. |
| `Which location led sales?` | Correctly returns `Maitama`. |

The handler recognized narrowly defined phrases such as `lowest revenue`, `least revenue`, `lagging`, and `far behind`. It did not recognize the ordinary equivalent `lowest sales`; its location gate also favored phrases such as `which location` rather than the equally natural `what is the location`.

This was a verified backend defect. The observed highest-sales response likely happened after this handler returned `None` and the turn fell through to a generic route or model response. The exact live transcript would identify that subsequent branch, but the deterministic defect existed independently of it. The direct saved-insight case is now covered by regression tests; the remaining work is to replace the broader heuristic architecture described below.

## Current Backend Request Flow

```text
Browser or LiveKit
  -> Gateway / stream endpoint
  -> authenticated tenant and profile metadata
  -> workspace-specific host runtime
  -> host processor
     -> direct deterministic insight or clarification path
     -> specialist selection and Kafka delegation
     -> specialist processor
        -> tool shortlist and LLM tool loop
        -> tenant-scoped MCP service(s)
     -> host result / business-review composition
  -> A2A artifacts and task status events
  -> chat and voice presentation layers
```

| Layer | Current responsibility | Verified strengths | Material limitation |
| --- | --- | --- | --- |
| Gateway | CORS, JWT, direct stream history, task forwarding, chat APIs. | Carries text history and structured payloads into direct stream metadata. | Direct-client history, persisted chat history, and optional context memory are separate inputs rather than one authoritative conversation state. |
| Shared runtime | Builds profile-specific host/specialist workers and injects control-plane configuration. | Uses workspace runtime names and per-agent tool bindings. | Availability is still partly based on an eventually consistent Kafka directory and fallback paths. |
| Host | Clarifies, selects a specialist, handles named insights, composes multi-agent reviews. | Has specific flows for sales, inventory, import, procurement, and business review. | Most behavior lives in a very large procedural processor with overlapping classifiers. |
| Specialist | Executes domain workflow and creates typed insight payloads. | Tool calls are tenant-scoped and MCP allow-listed. | Tool output is not normalized into a shared analytical fact model before response generation. |
| MCP | Performs service-specific read and write actions. | POS supports date-windowed sales summaries, product trends, and current location refresh in A2A. | Contract semantics differ per MCP and errors become generic model context. |
| JEV | Observes or selects bounded decisions. | Provider-neutral facade, probability/confidence fields, timeout, retry, circuit breaker, and tenant attribution exist. | Local configuration is shadow-only, and structured tool-derived answers do not receive an enforceable answer-quality gate. |
| LiveKit | Buffers speech and delegates to host. | Final speech fragments can be merged and unclear utterances can receive a clarification. | Voice and text share a host context but still depend on the same weak semantic follow-up layer. |

## What Is Already Working

| Capability | Evidence |
| --- | --- |
| Workspace and tenant propagation | `ToolContext` and MCP authorization are built from request metadata rather than frontend-selected tenant values. |
| Structured result transport | A2A supports `DataPart` artifacts and the host can emit typed `insight_response` payloads. |
| Sales data aggregation | `pos.get_sales_summary` returns date, day count, groupings, total sales, and total quantity sold across completed order lines. |
| Current location display correction | POS location labels can be refreshed against the inventory location service before A2A creates a sales insight. |
| Clarification persistence | Host clarification workflows save the original request and can merge a later time-range answer. |
| Basic follow-up reuse | Saved insight widgets can answer some location grouping, revenue, product, risk, and action questions without another tool call. |
| Bounded JEV infrastructure | JEV has a provider-neutral contract, request redaction/truncation, tenant/task/context attribution, failure fallback, and structured telemetry. |
| Voice safety baseline | Final utterances can be buffered, merged, clarified, cancelled, and delegated to the host instead of blindly forwarding each interim fragment. |

## Capability Gaps

### P0 - Semantic Follow-up Direction Is Only Partially Represented

**Current status:** The canonical fact path now builds a bounded frame with
domain, entity, metric, ranking direction, and time-scope detection. It can
reuse an unambiguous prior frame and validates the selected facts before
returning a minimum, maximum, or zero-ranking answer. Legacy widget parsing
remains only as a compatibility fallback for response types that do not yet
emit canonical facts. See [fact frame resolution](../src/kafka_a2a/analytics_facts.py#L191)
and [selection validation](../src/kafka_a2a/analytics_facts.py#L234).

**Remaining impact:** Equivalent wording outside the supported ranking set can
still require a fresh query or clarification. The previous
`lowest sales` versus `highest sales` inversion is covered for canonical
location/product facts, but the same formal frame is not yet used by every
host route, tool plan, and specialist response.

**Required capability:** A typed `AnalyticIntent` or `QueryFrame` must be created before reuse or delegation. It must contain at least domain, entity/dimension, metric, aggregation, ranking direction, time window, location scope, requested comparison, and turn relation.

**Acceptance criterion:** Every request for highest, lowest, least, most, top, bottom, weakest, strongest, lagging, and leading is mapped to a canonical comparison direction. The response renderer must reject an answer whose cited fact has the opposite direction.

### P0 - Legacy Saved Widgets Still Act as a Compatibility Query Engine

**Current status:** New structured insights persist a versioned fact packet and
the host prefers it before inspecting widgets. It declines global-minimum
claims from truncated top-seller evidence. Widget parsing still exists only to
support older text-only or pre-fact-packet tasks. See
[fact-packet follow-up handling](../src/kafka_a2a/analytics_facts.py#L449)
and [legacy compatibility fallback](../src/kafka_a2a/langgraph_processor.py#L1398).

**Impact:** A chart, ranked list, or newly renamed widget can alter answer semantics. Multiple completed reports in one conversation can cause the wrong payload to be selected. The backend cannot explain exactly which source query, row, metric, or sort direction justified a follow-up answer.

**Required capability:** Persist a compact immutable `FactPacket` alongside every analytical response. It should contain source tool name, sanitized arguments, source timestamp, canonical rows, metric definitions, time window, entity identifiers, and data freshness. Widgets should render from a fact packet, not become the fact packet.

**Acceptance criterion:** A follow-up can either answer directly from a matching fact packet or explicitly re-query. It never ranks a display-only structure without provenance.

### P0 - Deterministic Answer Validation Is Scoped to Canonical Ranking Follow-ups

**Current status:** Direct fact-backed ranking answers now validate selected
facts against the frame's requested direction before response composition.
Tool-backed `DataPart` responses and free-form delegated text do not yet share
one universal final-answer contract. See
[ranking selection validator](../src/kafka_a2a/analytics_facts.py#L234), the
[final-response branch](../src/kafka_a2a/langgraph_processor.py#L21829), and
[JEV evaluation](../src/kafka_a2a/decisioning/facade.py#L303).

**Impact:** The system has no last line of defense against a response that answers the wrong metric, time range, entity, location, or ranking direction. A fluent but wrong answer can be completed successfully.

**Required capability:** Add a deterministic response contract check before a result is emitted. For analytics it must verify the answer against the `QueryFrame` and `FactPacket`: requested metric, entity, time window, filters, ranking direction, and source completeness. JEV can provide a second probabilistic evaluation, but cannot replace these deterministic checks.

**Acceptance criterion:** A response to `lowest sales location` cannot mention a location that is not the minimum sales row in the cited fact packet. On mismatch, the host re-queries or returns a transparent inability message, never a confident opposite answer.

### P0 - JEV Is Not Yet Protecting Production Behavior

**Evidence:** The inspected local A2A environment configures JEV as `enabled=true`, `provider=typesafe`, `mode=shadow`, and `model=jev-latest`. In shadow mode the facade submits observations and preserves the baseline. See [DecisioningConfig](../src/kafka_a2a/decisioning/config.py#L52), [AgentDecisionFacade](../src/kafka_a2a/decisioning/facade.py#L74), and [DecisionService](../src/kafka_a2a/decisioning/service.py#L62).

**Impact:** JEV can reveal divergence in logs but cannot correct host-turn classification, specialist selection, tool shortlisting, or final answer quality. This is appropriate while validation data is immature, but it must not be described as active quality protection.

**Required capability:** Keep JEV shadow-only until the deterministic analytics contract exists and a measured evaluation set proves improvement. Then enable only narrow, reversible decisions with confidence thresholds and kill switches.

**Acceptance criterion:** Any JEV-enforced decision is bounded to pre-authorized candidates, records its baseline and chosen result, and falls back deterministically when unavailable, low-confidence, or contradictory to the answer contract.

### P1 - Host Planning Is Implemented as Overlapping Procedural Heuristics

**Evidence:** `langgraph_processor.py` contains host direct-insight detection, domain inference, strong overrides, specialist selection, stored-workflow continuation, multi-agent orchestration, response composition, and tool-loop logic. The actual `StateGraph` used in the generic model path is a short model/tool loop, while much of orchestration is managed through conditionals and persisted dictionaries. See [processor construction](../src/kafka_a2a/langgraph_processor.py#L15857) and [tool loop](../src/kafka_a2a/langgraph_processor.py#L21829).

**Impact:** A single turn can be classified independently by named-insight rules, strong-domain overrides, saved workflow state, historical insight logic, specialist selection, LLM tool choice, and JEV observation. Disagreement is not represented as an error state. This makes it hard to reason about why a turn was routed and why a follow-up ignored earlier context.

**Required capability:** Introduce an explicit host state machine with stages: normalize, classify relation, build `QueryFrame`, resolve context, plan, execute, validate, compose, and complete. The planner should produce a durable structured plan, not only prompt text and branch-local dictionaries.

**Acceptance criterion:** Each completed task contains a machine-readable execution record naming the intent, selected data sources, specialist plan, tool calls, evidence packets, retries, and final validation result.

### P1 - Specialist Results Are Not Semantically Validated Before Composition

**Evidence:** Specialist result evaluation exists as a JEV observation, but ordinary results are passed through from delegated artifacts or text. A result can be partial, stale, or off-topic while still being included in a business review. See [_decisioning_observed_proc](../src/kafka_a2a/langgraph_processor.py#L22035) and [specialist-result policy](../src/kafka_a2a/decisioning/policy.py#L30).

**Impact:** A multi-domain review can look complete despite missing a required domain, using an unsupported section, or mixing different time windows.

**Required capability:** Define a `SpecialistResultContract` for each planned step. It must specify mandatory facts, source freshness, requested time window, allowed warnings, and expected widget/fact types. The host validates every step before accepting it into the review.

**Acceptance criterion:** The final business review lists completed, unavailable, and omitted domains explicitly. It cannot label a review complete if a required domain has no valid evidence.

### P1 - MCP Contracts Do Not Expose One Common Analytics Vocabulary

**Evidence:** POS returns `groups`, `locations`, `products`, and service-specific field names. The host normalizes them into widgets with still different keys such as `label` and `value`. MCP execution is correctly allow-listed and tenant-scoped, but result schema normalization occurs ad hoc in each builder. See [POS sales summary](../../pos_backend_service/mcp_server/server.py#L627), [POS product trend](../../pos_backend_service/mcp_server/server.py#L790), and [POS insight builder](../src/kafka_a2a/langgraph_processor.py#L7896).

**Impact:** Cross-agent analysis and generic follow-up require heuristic field probing. It also makes an analytics response fragile when an MCP evolves.

**Required capability:** Establish a versioned analytics schema shared by MCPs and A2A. At minimum: `metric`, `unit`, `currency`, `dimension`, `entity_id`, `entity_label`, `value`, `sort_order`, `window`, `source`, `freshness`, and `completeness`.

**Acceptance criterion:** The host can ask every analytical MCP for a canonical tabular fact set and derive ranking, comparisons, widgets, PDFs, and voice summaries from the same data.

### P1 - Tool Failure Recovery Is Generic Rather Than Operation-Aware

**Evidence:** MCP HTTP calls have timeouts and useful logging. A tool exception becomes a `ToolResultPart` with an error string, then is supplied to the model for another turn. Multi-MCP resolution can skip unavailable executors, but there is no operation-specific retry, degraded-read policy, or source alternative contract. See [MCP session execution](../src/kafka_a2a/mcp_tools.py#L491), [configured tool call](../src/kafka_a2a/mcp_tools.py#L1599), and [model tool loop](../src/kafka_a2a/langgraph_processor.py#L21829).

**Impact:** The model may retry an unsuitable tool, ask an unrelated question, or complete an answer with partial data after a transient failure.

**Required capability:** Classify tool operations as read, idempotent write, or non-idempotent write. Define per-operation retry budgets, circuit behavior, fallback source(s), and an explicit `partial_data` outcome.

**Acceptance criterion:** A failed read either retries safely, returns a cited partial result, uses an approved fallback, or reports the unavailable source. A failed write is never retried without an idempotency key and an explicit policy.

### P1 - Conversation State Has Multiple Authorities

**Evidence:** The gateway accepts supplied direct-stream history; the chat API persists conversation details separately; contextual memory is optional and defaults to off unless configured; host workflow state is saved inside context memory. See [gateway history attachment](../src/kafka_a2a/server/gateway.py#L126), [context-memory configuration](../src/kafka_a2a/langgraph_processor.py#L15903), and [workflow-state persistence](../src/kafka_a2a/langgraph_processor.py#L15984).

**Impact:** The same follow-up can behave differently in browser chat, a resumed session, a direct stream, or voice. A caller can also have correct raw history but stale/missing workflow state or analysis memory.

**Required capability:** Define a single server-owned `ConversationState` record per context with append-only turns, active task, latest validated fact packets, pending clarification, active plan, and selected scope. Clients may submit a last-seen sequence number but should not be the authoritative historian.

**Acceptance criterion:** A follow-up produces the same backend `QueryFrame` whether it arrives through text, voice, a reconnect, or a browser refresh.

### P1 - Time and Scope Are Not Fully Tenant-Aware in the Analytics Layer

**Evidence:** Insight time-window parsing uses UTC calendar dates. POS queries use date windows based on request date and service-level time handling. Current location names receive a best-effort A2A refresh after POS returns snapshot labels. See [_resolve_insight_time_window](../src/kafka_a2a/langgraph_processor.py#L477) and [_refresh_sales_location_labels](../src/kafka_a2a/langgraph_processor.py#L7852).

**Impact:** `today`, `this week`, and month/quarter boundaries can differ from the workspace's operating timezone. Location names can still become stale if the refresh service is unavailable, and the original POS response does not carry an authoritative name-resolution version.

**Required capability:** Carry workspace timezone and canonical structural-location IDs in the `QueryFrame` and fact packet. Resolve display labels from an authoritative location projection at render time, with a documented stale-data warning if that projection is unavailable.

**Acceptance criterion:** A workspace's `today` means its own business day, and every location answer includes a stable location ID even when the display name changes.

### P2 - The Test Corpus Measures Presence More Than Correctness

**Evidence:** The generated corpus contains 300 scenarios, but the unit test verifies corpus size and metadata. The live smoke runner mostly checks expected or rejected text markers and typed-widget markers. There is no regression case for the natural-language inverse follow-up `What is the location with the lowest sales?`. See [test_eval_corpus.py](../tests/test_eval_corpus.py) and [a2a_live_smoke.py](../scripts/a2a_live_smoke.py).

**Impact:** A response can contain `metric_grid` and sales text while still returning the wrong entity or reverse ranking direction.

**Required capability:** Add golden fact fixtures and semantic assertions for entity, metric, direction, time window, scope, factual value, supported source, and task state. Include multi-turn and topic-switch suites.

**Acceptance criterion:** CI executes deterministic follow-up tests against fixtures and fails on a highest/lowest inversion even when the response shape is valid.

### P2 - Observability Does Not Yet Form a Full Answer Trace

**Evidence:** JEV emits structured telemetry with profile, task, context, capability, decision, probability, confidence, latency, and token usage. MCP operations emit useful logs. There is no unified durable record tying a user turn to the selected intent frame, raw evidence, specialist result validation, final answer contract, and outcome feedback. See [decision telemetry](../src/kafka_a2a/decisioning/telemetry.py#L15).

**Impact:** Debugging relies on individual service logs and conversation exports rather than one traceable explanation of a wrong answer.

**Required capability:** Emit an OpenTelemetry-compatible or durable `A2AExecutionTrace` across gateway, host, delegation, MCP, validation, rendering, and voice. Redact user secrets but retain hashes, IDs, source versions, and semantic decisions.

**Acceptance criterion:** Given a task ID, support can see why `Maitama` was selected, which row was compared, whether the requested operation was `min` or `max`, and whether an evaluator approved it.

## Why the Existing Test Suite Did Not Catch This

The existing suite validates many important paths, including direct sales-by-location queries, current location display refresh, business-review composition, and some historical follow-ups. The missing class is semantic paraphrase and inverse-operation testing.

The suite currently has direct wording such as `Which branch is underperforming in sales today?`, `Which location sold the most this month?`, and `lowest revenue`. It does not cover the conversation sequence below against the same stored facts:

```text
User: Analyze my sales data for the last three weeks.
Assistant: [location contribution result]
User: What is the location with the lowest sales?
Expected: the minimum-sales location from the prior period, with value and period.
Rejected: any highest/leading location unless the user asks for it.
```

The correct test strategy is not adding one more phrase to a regular expression. It is asserting that all wording variants compile to the same query frame and then validating the final answer against a fact fixture.

## Target Architecture

```text
Inbound turn
  -> TurnRelation classifier
  -> QueryFrame builder
  -> ConversationState resolver
  -> Evidence reuse decision: use fact packet / re-query / clarify
  -> ExecutionPlan builder
  -> Specialist and MCP execution
  -> Normalized FactPackets
  -> Deterministic contract validation
  -> JEV evaluation where it adds value
  -> Response composer
  -> Final answer contract validation
  -> Artifacts, trace, and state update
```

| Current pattern | Target pattern |
| --- | --- |
| Phrase checks determine `max` or `min`. | Canonical operation enum determines ranking direction. |
| Widgets are mined for facts. | Fact packets are the source; widgets are projections. |
| Multiple classifiers can disagree silently. | One query frame is passed through every stage and disagreement is recorded. |
| Specialist completion is treated as usable output. | Every plan step has a result contract and validation status. |
| JEV shadows baseline behavior. | JEV remains bounded and layered after deterministic safety checks. |
| Errors are presented to the LLM as text. | Errors are typed, retried or degraded under an explicit policy. |
| Context comes from several optional stores. | One backend conversation state owns active plan and evidence. |

## Recommended Implementation Sequence

### Phase 0 - Contain the Current Failure

1. Add deterministic tests for lowest/highest, top/bottom, least/most, strongest/weakest, and `what is the location` wording against one fixed fact packet.
2. Add a `LocationSalesFollowUp` parser that produces metric `sales_revenue`, entity `location`, operation `min` or `max`, and inherited timeframe.
3. Route these follow-ups directly to a canonical fact packet or `pos.get_sales_summary(group_by=location)`.
4. Validate the selected row before emitting text or a widget.
5. Do not broaden the current regular-expression lists as the primary fix; retain them only as a compatibility fallback during migration.

### Phase 1 - Make Analytics Evidence First-Class

1. Define versioned `QueryFrame`, `FactPacket`, `AnswerContract`, and `SpecialistResultContract` models.
2. Make POS emit a canonical location-sales fact packet with structural location ID, resolved name, revenue, units, order count, period, timezone, currency, and sort order.
3. Adapt inventory, purchasing, products, users, and audit MCP outputs to the same envelope where they provide analytics.
4. Store the last validated fact packets in server-owned conversation state.
5. Render widgets, PDF exports, and voice summaries from the exact same fact packet.

### Phase 2 - Rationalize Host Orchestration

1. Split intent parsing, context resolution, planning, execution, validation, and composition out of the monolithic processor.
2. Persist a durable plan with step IDs, dependencies, retry policy, expected contract, and completion status.
3. Validate each specialist result before adding it to a business review.
4. Add cancellation and supersession to stop stale plans when a user starts a new request.
5. Expose one user-facing progress model from the durable plan rather than raw agent lifecycle text.

### Phase 3 - Add Controlled JEV Enforcement

1. Keep current shadow telemetry and build dashboards for disagreement, confidence, latency, fallback rate, and sampled incorrect outcomes.
2. Enable JEV enforcement only for turn relation and specialist selection after the corresponding evaluation suite achieves its agreed pass rate.
3. Keep direct authorization, confirmation, mutation execution, tenant scope, and MCP argument injection deterministic permanently.
4. Add JEV final evaluation only after `AnswerContract` validation, using the fact packet as evidence and allowing only `accept`, `re-query`, or `ask_clarification` outcomes.
5. Gate every enforced capability with independent confidence threshold, timeout, circuit breaker, shadow comparison, and immediate rollback flag.

### Phase 4 - Reliability and Evaluation

1. Introduce operation-aware MCP retry and degradation contracts.
2. Make workspace timezone and structural location identity mandatory in analytics queries.
3. Build a multi-turn evaluation dataset from anonymized real failure classes and fixture facts.
4. Run deterministic semantic tests in CI; run live smoke tests separately against a seeded environment.
5. Persist an end-to-end trace for every failed, retried, incomplete, or user-corrected task.

## Test Matrix Required Before Claiming Conversational Analytics Readiness

| Area | Minimum cases |
| --- | --- |
| Ranking semantics | Highest, lowest, least, most, top, bottom, leading, lagging, ties, zero rows, one row. |
| Follow-up relation | Direct follow-up, pronouns, topic switch, clarification answer, cancellation, corrected request, stale context. |
| Time | Today in workspace timezone, explicit dates, last N days, prior calendar month, quarter, year, all time. |
| Scope | All locations, one location, renamed location, no structural location, role-limited location. |
| Evidence | Saved fact reuse, re-query due stale data, partial source, failed source, conflicting sources. |
| Multi-agent | All planned steps succeed, one unavailable, one invalid, one slow, one cancelled, final synthesis incomplete. |
| JEV | Shadow divergence, low confidence, timeout, malformed result, circuit open, rollback to deterministic baseline. |
| Voice | Fragment merge, clarification answer, topic change, host result, no silent unsupported turn, text/voice shared context. |

## Definition of Done for This Gap Class

The service is ready for this class of analytics conversation only when all statements below are true:

1. Every analytical turn has a stored semantic query frame.
2. Every number, ranking, and comparison in the answer maps to a fact packet and source query.
3. A minimum/maximum answer is deterministically checked against the selected metric before delivery.
4. Follow-ups use backend-owned conversation state rather than frontend history alone.
5. Structured insight responses receive the same quality checks as plain text responses.
6. JEV is an additional bounded evaluator, not the sole guard against semantic errors.
7. CI contains the exact reported failure as a regression test and tests paraphrase variants.
8. A task trace can explain a wrong answer without reconstructing multiple container logs manually.

## Files Most Likely to Change in a Future Implementation

| File or module | Expected responsibility |
| --- | --- |
| `src/kafka_a2a/langgraph_processor.py` | Extract semantic parsing and follow-up handling into smaller dedicated modules. |
| `src/kafka_a2a/analytics/contracts.py` (new) | Define query frames, fact packets, answer contracts, and validation results. |
| `src/kafka_a2a/conversation_state.py` (new) | Own server-side active plan, clarification, and validated evidence state. |
| `src/kafka_a2a/orchestration/` (new) | Plan, execute, validate, and compose multi-agent tasks. |
| `src/kafka_a2a/mcp_tools.py` | Add typed tool failure policy and operation-aware retry/degradation. |
| `src/kafka_a2a/decisioning/` | Add evidence-aware bounded decision capability after deterministic contracts exist. |
| `pos_backend_service/mcp_server/server.py` | Emit canonical analytics facts and workspace-aware time/location metadata. |
| `tests/test_langgraph_processor.py` | Add deterministic semantic follow-up and answer-contract regressions. |
| `tests/test_eval_corpus.py` and `scripts/a2a_live_smoke.py` | Execute semantic assertions, not only output-shape checks. |
| `tests/fixtures/analytics/` (new) | Hold stable cross-service fact fixtures for CI. |

## Audit Boundary

The initial audit was static. The hardening work recorded in this document was
implemented afterward and is covered by the verification results above. It does
not replace the required live acceptance matrix: browser streaming, seeded-data
analytics, LiveKit audio, and deployed CORS behavior still need a running
environment before they can be marked as production-verified.
