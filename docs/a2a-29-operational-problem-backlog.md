# A2A Operational Problem Backlog

This is the implementation backlog extracted from the observed demo failures,
follow-up failures, and operational questions. It is deliberately separate
from the 300-question insight benchmark: these are user problems to solve,
not only prompts to classify.

## Acceptance Rules

- Every read answer must use tenant-scoped evidence and identify its time window.
- A missing required scope must produce one targeted clarification, not a menu of unrelated actions.
- Read-heavy answers must return typed widgets; raw JSON, Python part representations, and duplicated messages fail acceptance.
- Mutating actions must be previewed and confirmation-gated.
- Follow-ups must reuse the active conversation context unless the user clearly changes topic.
- A failed or unavailable specialist must return a user-facing explanation and a recoverable next step.

## Problem Matrix

| ID | Problem | Primary area | Acceptance target | Status |
|---|---|---|---|---|
| P01 | POS product is missing from the cashier view | POS/product | Ask for product name, SKU/barcode, and terminal/location, then diagnose catalog, variant, visibility, price, stock, sync, and permission state | Automated verified; browser pending |
| P02 | Inventory analysis is asked without a time window | Host/inventory | Ask for one time window before historical analysis; current stock snapshots remain immediate | Automated verified; browser pending |
| P03 | Supplier delay analysis is asked without a time window | Procurement | Ask only “What time frame should I use?” and continue after the answer | Automated verified; browser pending |
| P04 | Purchase-order drafting is routed to marketplace search | Routing | Draft PO language always routes to procurement and never opens an external marketplace | Automated verified; browser pending |
| P05 | Structured procurement data appears as raw JSON | Contract/UI | Return supplier-grouped comparison/timeline/risk widgets | Automated verified; browser pending |
| P06 | A raw text part and formatted answer are both rendered | Frontend | Render one canonical assistant response per event | Automated verified; browser pending |
| P07 | Multi-agent answers are flattened into one unreadable block | Host/UI | Preserve specialist sections and append one overall summary | Automated verified; browser pending |
| P08 | Graphs waste PDF space and split across pages | Export | Start each graph on a new landscape page | Automated verified; browser pending |
| P09 | Business reviews repeat the same stock risk in several widgets | Composition | Collapse exact duplicate widgets and avoid duplicate facts across sections | Automated verified; browser pending |
| P10 | Renamed locations appear under stale names | Cross-service scope | Resolve current tenant location labels at response time; never use historical labels as current truth | Automated verified; browser pending |
| P11 | Voice reads Naira values as dollars | Voice/currency | Speak the response currency from the structured currency field | Automated verified; browser pending |
| P12 | Voice follow-ups lose the active analysis context | LiveKit | Reuse the same session fact packet and route follow-ups consistently | Automated verified; browser pending |
| P13 | Voice gives silence for unsupported requests | LiveKit | Return a concise fallback such as “I can’t complete that yet” with a next step | Automated verified; browser pending |
| P14 | A follow-up is mistaken for a new unrelated task | Host memory | Distinguish elliptical follow-ups from explicit topic changes | Automated verified; browser pending |
| P15 | Agent setup remains pending indefinitely | Agent setup | Surface lifecycle state, timeout cause, and retry action instead of an endless spinner | Automated verified; browser pending |
| P16 | Provider setup lacks API-key links | Frontend/settings | Show provider-specific official setup links without storing provider keys in UI state | Automated verified; browser pending |
| P17 | A2A requests fail due to missing CORS headers | Gateway/deployment | Allow configured frontend origins and pass preflight checks | Automated verified; browser pending |
| P18 | Fast UI timeout claims the agent is too slow | Frontend/streaming | Keep the request open until server completion or explicit cancellation | Automated verified; browser pending |
| P19 | Business analysis reaches the sensitive user service | Host/tools | Use audit data for activity evidence; do not call user service for analytics | Automated verified; browser pending |
| P20 | Terminal names are mistaken for products | POS/entity resolution | Preserve entity type and explain terminal/location context explicitly | Automated verified; browser pending |
| P21 | Existing inventory is reported as a new catalog opportunity | Product/import | Distinguish importable catalog candidates from already-imported inventory | Automated verified; browser pending |
| P22 | Product and variant images are omitted from opportunity widgets | Product/UI/export | Use the product or variant primary image when available, then a deterministic fallback | Automated verified; browser pending |
| P23 | Plain numeric fields are formatted as money | Contract/UI | Require explicit `format: currency` for money; counts remain numbers | Automated verified; browser pending |
| P24 | Native selects close the AI widget or use unreadable styling | Frontend/forms | Use the shared select component and stop event propagation inside the widget | Automated verified; browser pending |
| P25 | Chat reset races with the first submitted prompt | Frontend/chat | Disable submit until reset is complete | Automated verified; browser pending |
| P26 | Demo tests require refreshing and lose conversation history | Frontend/chat | Run the full prompt set in one persistent conversation | Automated verified; browser pending |
| P27 | Text parts show escaped newlines and internal labels | Contract/UI | Normalize line breaks and remove transport/debug labels before display | Automated verified; browser pending |
| P28 | Permission and audit answers lack actionable evidence | Audit/security | Return timeline/risk widgets with actor, action, object, and time window | Automated verified; browser pending |
| P29 | Role changes and notifications can be implied as completed | Users/actions | Preview the mutation, require confirmation, then report the actual result | Automated verified; browser pending |

## Current Iteration

The first implementation slice normalizes structured responses at the A2A
boundary by removing only byte-for-byte duplicate widgets and recording a
warning. This fixes repeated panels without hiding distinct evidence. The
second slice makes procurement money fields explicit and prevents generic
numeric `value` fields from becoming currency. The third slice replaces the
chat action form's native select with the shared React Select component and
stops pointer propagation at the form boundary. The fourth slice hardens the
frontend structured-output boundary: an attached transport wrapper no longer
prevents the renderer from parsing the typed insight in the message text, and
balanced JSON embedded after a status prefix is converted into widgets instead
of a code block. Existing routing, location label refresh, image propagation,
and timeframe clarification tests cover the corresponding observed failures.

The fifth slice adds a published-demo contract regression suite covering all
20 landing-page prompts: deterministic read prompts retain their specialist
intent, warehouse and procurement workflows preserve their inventory routing,
role and notification prompts stay with Users, and contextual follow-ups are
not misclassified as fresh named insights. The full A2A suite now passes 527
tests. The separate Users entitlement fix for LiveKit voice access is deployed
through the Users release path, which creates `oral_conversation_with_ai`,
syncs role/group presets, and invalidates the permission cache.
Browser click-through remains pending because local browser automation is
disabled in the current session; the user's port-3000 frontend is not started
or stopped by this QA run.

## Verification Commands

```bash
cd /Users/ubongpr7/dev/pr7/inventory/kafka_a2a
./.venv/bin/python -m pytest -q tests/test_analytics_facts.py tests/test_langgraph_processor.py tests/test_insight_upgrade_contract.py

cd /Users/ubongpr7/dev/pr7/inventory/intera_f
node --test ./tests/agent-structured-output.test.ts
yarn typecheck
yarn test:agent-export
yarn build
```

