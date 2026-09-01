# Prompt For The Next Kafka A2A Agent

You are taking over Kafka A2A work in:

`/Users/ubongpr7/dev/pr7/inventory/kafka_a2a`

Read this handoff document first:

`/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/docs/a2a-production-readiness-handoff.md`

Then inspect these already-modified files before making changes:

- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/agent_filter.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/langgraph_processor.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/livekit_voice/worker.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/src/kafka_a2a/local_tools.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/fake_langgraph_components.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/test_agent_filter.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/test_langgraph_processor.py`
- `/Users/ubongpr7/dev/pr7/inventory/kafka_a2a/tests/test_livekit_voice_worker.py`

Your job is to make the Kafka A2A system production-ready for both text and voice.

Primary objectives:

1. Fix voice-agent utterance handling.
2. Stop malformed or incomplete voice fragments from reaching the host.
3. Make the host ask targeted follow-up questions instead of failing generically.
4. Add real planning for multi-agent requests like business-performance analysis.
5. Make “import products” mean global-catalog import by default.
6. Fix MCP global product import execution and local agent-registration reliability.
7. Ensure A2A widgets are rendered once, from the A2A side only, while voice remains transcript-only.

Critical examples that must work:

- “Can you analyze my sales data?” followed by “past one year”
- “Analyze my business performance for the last quarter”
- “I want to import products”

Important implementation rules:

- Do not work on unrelated Kotlin or non-A2A tasks.
- Do not assume dev/prod and local have the same failure mode.
- Do not break dev/prod while fixing local.
- Prefer tests first for voice clarification merge, host clarification merge, business-review planning, product-import routing, and widget dedupe.

Expected behavior:

- The voice agent should buffer, normalize, and clarify before delegation.
- The host should classify single-agent vs multi-agent vs clarification-needed requests.
- Business review requests should produce an orchestration plan across relevant specialists.
- Product import should begin with global catalog filters: category xor brand.
- MCP failures should return precise, actionable errors.

When you start, first summarize:

- what is already implemented
- what is partially implemented
- what is broken now
- what you will fix first

Then execute in this order:

1. voice clarification and merge
2. host clarification and planning
3. product-import routing
4. MCP import execution
5. local registration reliability
6. transcript/widget dedupe

Do not stop at analysis. Make code changes, run focused tests, and state exactly what remains.
