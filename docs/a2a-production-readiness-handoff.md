# Kafka A2A Production Readiness Handoff

Last updated: August 18, 2026

## Purpose

This document is the handoff plan for the next agent working on `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a`.

The immediate goal is to get the Kafka A2A system to a production-ready state for:

- text chat through the host agent
- voice chat through the LiveKit voice agent
- correct host-to-specialist delegation
- correct follow-up question handling
- reliable MCP-backed specialist tooling
- correct product-import workflow from the global catalog

This document only covers Kafka A2A and tightly related integration behavior. It does not cover unrelated frontend polish, Kotlin work, or other product areas.

## Current Known Working State

- The base A2A text chat path works in some scenarios.
- The LiveKit voice agent can connect and speak in at least some local/dev paths.
- Voice-side status updates can be spoken and can appear inside the voice transcript.
- The host can sometimes ask follow-up questions for time range and metrics.
- Product import workflow scaffolding exists and can render selection widgets in some paths.

## Current Known Failures

### 1. Voice agent sends incomplete or malformed user intent

The voice agent is still too eager to send partial or low-confidence utterances to the host.

Observed failures:

- It concatenates fragments that do not make sense.
- It sends low-confidence phrases directly to A2A instead of clarifying first.
- It can ask for clarification, but it does not reliably reformulate the final confirmed request before delegation.
- Example of bad voice-side merge behavior: user intended something like sales analysis over one year, but voice-side text became nonsense and was forwarded anyway.

Required behavior:

- The voice agent must wait for the user to finish speaking.
- It must maintain a pending utterance buffer.
- It must try to normalize the utterance into a coherent request.
- If confidence is low, it must ask a clarification question before sending anything to host.
- If it proposes a rephrased version, it should confirm with the user before delegation.
- Only the confirmed, normalized request should be sent to the host agent.

### 2. Host agent still delegates too early or to the wrong specialist

The host is still too willing to delegate without proving it understands the request.

Observed failures:

- “Analyze my business performance” can be mishandled.
- “Analyze my sales data for the past one year” has previously fallen into wrong routes.
- The host can still send vague requests to a single specialist instead of planning across multiple specialists.
- In some states the host asked the right follow-up question, but after the user answered it, the next step still failed or routed incorrectly.

Required behavior:

- The host must reject ambiguous requests early.
- The host must ask clarifying questions when scope, time range, entity, or metric is missing.
- The host must classify whether a request is:
  - single-agent
  - multi-agent
  - clarification-needed
  - unsupported
- The host must plan multi-agent work before delegation.

### 3. Business-performance analysis is not truly orchestrated

“Analyze my business performance” is not a single-agent question.

Expected orchestration:

- host identifies this as a composite analysis request
- host builds a plan
- host decides which specialists are needed
- host delegates sub-analyses
- host waits for all required responses
- host reflects on the collected results
- host composes a single user-facing answer

Possible specialist participants:

- POS / sales agent
- purchasing / purchase-order agent
- inventory agent
- product/catalog agent
- audit or risk agent
- notification/subscription agent only if relevant

Required behavior:

- The host must not throw the whole request at only one agent.
- The host must be able to describe the plan to the user.
- The host must update progress while specialists are running.
- The final answer must be a unified business review, not a raw dump from one specialist.

### 4. Reflection layer is too weak

The system needs stronger reflection before results are shown to the user.

Observed failures:

- Wrong specialist chosen.
- Wrong interpretation of user request.
- Bad or partial result returned.
- Follow-up answers not merged correctly into the active task.

Required behavior:

- Before presenting a result, the host should validate:
  - did the specialist answer the intended question
  - is the time range correct
  - is the entity correct
  - is the scope correct
  - is the answer complete enough
- If not, the host should ask follow-up questions or retry with a better instruction.
- Reflection should happen before user-facing output, not after failure.

### 5. Product import workflow is still semantically wrong in multiple paths

When the user says “I want to import products,” the intended meaning is:

- import products from the global catalog into the workspace

It must not default to:

- AI image bulk creation
- spreadsheet bulk creation
- manual product creation

Required behavior:

- “import product(s)” must route to global catalog import workflow first
- the workflow should ask the user to choose exactly one filter mode:
  - product category
  - brand
- it should not ask for both category and brand in the same first-step selector
- it should then render a widget with selectable options
- options should support select-all
- once submitted, the widget must disable itself to prevent duplicate submission
- next it should fetch matching products from the global catalog
- then render product selection
- then import the selected product IDs through the MCP product import tool

### 6. MCP product import still fails in some paths

Known error observed:

- `product.import_global_catalog_products` failed through MCP

Observed failure mode:

- tool resolution or execution failure during session operation
- prior examples also showed `list_tools` failures against several MCP servers locally when those services were unhealthy or misconfigured

Required behavior:

- local and dev MCP health must be verified separately
- tool resolution should degrade cleanly
- actual import tool failures must show root cause
- product import workflow must not silently loop back to the first widget after an MCP failure

### 7. Local environment agent registration is unstable

Observed errors:

- `Requested agent 'pos' is not registered.`
- `Requested agent 'product' is not registered.`
- local gateway container unhealthy
- MCP server failures caused route resolution gaps

Likely causes:

- local stack not running all specialist agents
- gateway unhealthy due downstream dependency failure
- MCP list-tools failures preventing route exposure
- possible mismatch between local and dev/prod tool registry assumptions

Required behavior:

- local stack must start all required specialist agents
- host must only advertise delegations to registered agents
- route resolution must clearly report missing agents vs MCP unavailability
- next agent should verify docker compose agent coverage for host, pos, inventory, product, onboarding, purchasing, audit, subscriptions, notifications

### 8. Voice and A2A transcript sync is still fragile

Observed failures over time:

- voice transcript showed progress updates while the A2A chat showed nothing
- A2A chat sometimes showed duplicate widgets
- voice path and text path could get out of sync
- user had to start a normal A2A chat first before call-mode synced correctly in some runs

Current likely interpretation:

- session bootstrapping between voice-side and A2A-side is fragile
- widget rendering may happen twice when both voice transcript and A2A event stream try to render the same interactive result

Required behavior:

- the A2A chat is the source of truth for widgets
- the voice transcript should show conversational transcript only
- if the voice assistant narrates a widget event, it should not render a second copy of that widget
- a voice session should be able to start from a clean page without requiring a prior text message

### 9. Follow-up question handling is still incomplete

The system needs better follow-up behavior on both voice and host sides.

Required behavior:

- if the user answers a host clarification with something like “past one year,” the system must merge that into the original pending question
- the merged request should be visible as the active interpreted request
- short follow-up turns must not be treated as brand-new unrelated tasks
- if a user reply is still insufficient, the next question should explain exactly what is missing

## Dirty Files Already In Progress

These files are already modified locally and should be treated as the current work surface:

- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/agent_filter.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/langgraph_processor.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/livekit_voice/worker.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/local_tools.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/fake_langgraph_components.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/test_agent_filter.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/test_langgraph_processor.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/test_livekit_voice_worker.py`

The next agent should read these first before making new structural changes.

## Most Relevant Files To Inspect

- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/livekit_voice/worker.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/langgraph_processor.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/agent_filter.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/local_tools.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/mcp_tools.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/server/gateway.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/chat_store.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/context_memory.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/runtime/task_store.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/runtime/redis_task_store.py`

## Priority Order

### P0: stop bad delegation and bad voice forwarding

- strengthen voice-side utterance buffering
- require confidence before delegation
- add explicit clarification loop
- merge user clarification into the original request
- stop forwarding malformed concatenated utterances

Acceptance:

- user says “Can you analyze my sales data?”
- voice asks “What time range should I use?”
- user says “past one year”
- host receives the merged request “analyze my sales data for the past one year”
- host does not receive the raw fragment alone

### P0: restore host follow-up correctness

- host should ask targeted follow-up questions instead of generic failure
- host should not delegate incomplete requests
- host should not send business-performance requests straight to a single wrong agent

Acceptance:

- “Analyze my business performance” should trigger clarifying questions
- “Analyze my business performance for the last quarter” should produce a host plan, not a random product comparison

### P0: fix business review planning

- add explicit planning path for business review / business performance / business analysis requests
- identify all necessary sub-analyses
- orchestrate specialists
- reflect on all specialist outputs
- return one coherent final result

Acceptance:

- a business-performance request touches the right specialists
- final answer includes a coherent summary, not one narrow report

### P0: fix product-import intent routing

- “import product(s)” must map to global catalog import
- first widget must be filter mode only
- filter mode must be XOR, not both
- after filter selection, show the category list or brand list widget
- after that, show product selection widget

Acceptance:

- user says “I want to import products”
- no AI image, CSV creation, or manual-create path is offered first

### P0: fix MCP import execution and error transparency

- debug `product.import_global_catalog_products`
- inspect actual product MCP logs and request payload
- surface meaningful failure detail when import fails
- prevent looping back to the first selection step on execution error

Acceptance:

- selected global product IDs import successfully
- on failure, the user sees the real issue and the workflow state is preserved

### P1: fix local agent registration reliability

- verify all local specialists are running
- verify registry publication
- verify host route resolution
- make missing-agent errors operationally actionable

Acceptance:

- local host can delegate to pos, product, inventory, onboarding, purchasing, audit, notifications, subscriptions when those containers are up

### P1: fix voice/A2A transcript cohesion

- ensure voice-started sessions bootstrap an A2A thread automatically
- ensure A2A remains source of truth for widgets
- ensure voice transcript does not duplicate widgets
- ensure no prior text message is required before voice works correctly

Acceptance:

- a user can open voice first on a fresh page and still get synchronized A2A task output

### P1: improve progress narration

- when host delegates, voice should narrate it
- when specialists accept and begin work, voice should narrate it
- when widgets are rendered on the A2A side, voice should acknowledge the next step

Acceptance:

- voice updates are timely and match actual A2A task state

### P2: harden reflection and result validation

- verify specialist output matches the requested scope
- reject wrong or incomplete results before rendering
- ask for missing details instead of failing late

Acceptance:

- fewer “could not complete” fallbacks
- wrong-specialist outcomes do not leak to the user

## Recommended Implementation Flow

1. Read the dirty files listed above.
2. Write or tighten tests first for:
   - voice clarification merge
   - host clarification merge
   - business-performance planning
   - product-import intent routing
   - widget dedupe between voice and A2A
3. Fix voice-side buffering and confirmation logic.
4. Fix host-side ambiguity classification and planning.
5. Fix global product import routing and MCP execution.
6. Verify local agent registration and route resolution.
7. Re-test on:
   - local backend + local frontend
   - local frontend + dev backend
8. Only after stability, tune narration and polish.

## Suggested Test Scenarios

### Voice clarification

- “Can you analyze my sales data?”
- follow-up: “past one year”
- expected: merged request sent once

### Voice malformed speech

- deliberately noisy or fragmented speech
- expected: voice asks for clarification before delegation

### Business review

- “Analyze my business performance for the last quarter”
- expected: host plans multi-agent work

### Product import

- “I want to import products”
- expected: category-or-brand selection from global catalog

### Local registration

- “Can you help me analyze my sales data for the past one year?”
- expected: no `Requested agent 'pos' is not registered`

## Debug Commands To Keep Handy

Local Kafka A2A repo:

```bash
cd /Users/ubongpr7/dev/pr7/inventory/kafka_a2a
git status --short
```

Local stack:

```bash
cd /Users/ubongpr7/dev/pr7/inventory
docker compose -f docker-compose.local.yml -f docker-compose.ka2a-local.yml --profile ka2a ps
docker compose -f docker-compose.local.yml -f docker-compose.ka2a-local.yml --profile ka2a logs ka2a_gateway
docker compose -f docker-compose.local.yml -f docker-compose.ka2a-local.yml --profile ka2a logs voice-agent
```

Dev gateway logs:

```bash
cd /Users/ubongpr7/dev/pr7/inventory/infra/ecs-platform
set -a
source ../.env
source .env.dev
set +a
aws logs tail /ecs/interaims-dev/ka2a-gateway --region us-east-2 --since 30m --follow
```

## Explicit Non-Goals For This Handoff

- Do not work on Kotlin or unrelated screenshots.
- Do not widen scope into general mobile UI unless directly required by A2A behavior.
- Do not break dev/prod while fixing local.
- Do not assume frontend bugs are backend bugs without proving it.

## Definition Of Done For The Next Agent

The Kafka A2A service is ready for handback when all of the following are true:

- voice does not forward malformed or incomplete user intent
- host asks targeted follow-up questions instead of failing generically
- business-performance analysis uses planning/orchestration across the right specialists
- product import means global catalog import by default
- MCP global product import succeeds or fails with a precise actionable reason
- local agent registration is stable
- voice and A2A stay synchronized without duplicate widgets
- the user can start with voice only and still get correct A2A task results
