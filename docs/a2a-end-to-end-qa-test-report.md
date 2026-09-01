# A2A System QA Test Plan and Execution Report

**Date:** 2026-08-28  
**Environment:** Local KA2A Docker stack; signed-in FFG AI Debug Workspace (profile 4); browser at \`http://localhost:3005\`.  
**Scope:** Kafka A2A host routing, specialist delegation, conversation state, MCP-backed data, structured streamed insights, safe write flows, tenant isolation, and the LiveKit voice adapter. This is not a general frontend QA report.  
**Test method:** QA owns each case end-to-end: define the expected behavior, execute it, repair every reproducible defect, and rerun the relevant regression tests. Real workspace data is read-only during QA; import flows stop before final confirmation.

## Test Inventory

The suite contains 46 behavioral cases. Automated contract tests provide deterministic coverage of routing and boundaries; browser checks validate the signed-in integration path and rendered A2A payloads.

| Area | Cases | Method |
| --- | ---: | --- |
| Host entry and intent resolution | 8 | Automated and browser |
| Specialist routing and availability | 7 | Automated and browser |
| Conversation memory and follow-up | 6 | Automated and browser |
| Business review and structured insights | 6 | Automated and browser |
| Import and mutation safety | 5 | Automated and browser |
| MCP transport, authorization, and tenant scope | 6 | Automated |
| Stream and gateway resilience | 3 | Automated |
| Voice adapter contract | 5 | Automated |
| **Total** | **46** | |

## Execution Status

**Run QAI-01 (automated):** 2026-08-28. The focused A2A regression command completed with **347 passed, 1 existing warning** in 10.81 seconds. A result marked **Automated pass (QAI-01)** is backed by the named contract test in that run. The Product MCP contract was also run separately and passed: **2 passed**.

**Run QAI-02 (signed-in browser):** 2026-08-28. The browser used the existing authenticated workspace session. The following user journeys passed against live local services:

1. `Can you analyze my sales data?` produced the targeted time-range clarification, without starting POS analysis.
2. `The last three weeks.` merged into the sales request and returned 43 sales totaling N2,462,960.25 in rendered structured output.
3. `Which location needs the most attention from that review?` retained the prior three-week review and identified Agric, Ikorodu Store, rather than starting a new “today” report.
4. `I want to import products.` correctly broke out of sales-review context and opened the global-catalog category/brand selection flow without importing anything.
5. `Can you analyze my business performance for the last three months?` completed the four-domain review: Point of Sale, Inventory Management, User and Workspace Management, and Product Management. The rendered report contained separate typed domain sections under one parent business-review container.

**Manual-only boundary:** microphone/speaker behavior, device mute controls, and external provider billing remain manual acceptance checks. Mutating workflows were intentionally stopped before confirmation; no real import, stock adjustment, purchase order, reservation, or destructive action was made during QA.

## Detailed Acceptance Matrix

| ID | Scenario | Expected behavior | Evidence | Result |
| --- | --- | --- | --- | --- |
| HOST-01 | Greeting or plain help | Host answers locally without an unnecessary tool call or delegation. | \`test_is_simple_greeting_query_matches_plain_greetings\`; \`test_host_status_check_short_circuits_tool_and_llm_work\` | Automated pass (QAI-01) |
| HOST-02 | \`Can you analyze my sales data?\` | Host asks only for a time range before POS work starts. | \`test_host_direct_sales_analysis_request_requires_time_range_before_delegation\` | Automated + browser pass |
| HOST-03 | \`past one year\` after HOST-02 | Host merges the fragment with the earlier sales request and delegates to POS. | \`test_host_merges_time_range_follow_up_into_sales_analysis_request\`; \`test_host_sales_follow_up_merges_from_saved_clarification_workflow_state\` | Automated pass (QAI-01) |
| HOST-04 | Business review without dates | Host asks for a targeted business-review time range. | \`test_host_cross_domain_business_review_requires_time_range_before_delegation\` | Automated pass (QAI-01) |
| HOST-05 | Business review for last three months | Host creates a four-domain plan and completes it without a generic continuation prompt. | \`test_host_orchestration_plan_builds_cross_domain_business_review_flow\`; browser | Automated + browser pass |
| HOST-06 | Named sales, inventory, product, user, and procurement requests | Host selects the specific specialist rather than a generic router or unrelated agent. | \`test_select_host_delegation_agent_prefers_best_matching_specialist\`; \`test_host_procurement_payload_threads_calendar_windows_and_labels\` | Automated pass (QAI-01) |
| HOST-07 | Low-stock and out-of-stock questions | Current snapshots route directly to inventory and do not require a time range. | \`test_host_routes_stock_snapshot_questions_to_inventory_without_a_time_range\`; \`test_low_stock_summary_surfaces_out_of_stock_items\` | Automated pass (QAI-01) |
| HOST-08 | Explicit user correction | \`I did not say... show low stock\` replaces the mistaken intent before delegation. | \`test_host_rewrites_explicit_stock_transcript_correction\`; \`test_host_executes_the_corrected_low_stock_request_without_a_workflow_prompt\` | Automated pass (QAI-01) |
| ROUTE-01 | Sales by location | POS Admin is selected and returns a widget-first location result. | \`test_kafka_delegation_backend_prefers_pos_admin_for_sales_by_location_queries\`; \`test_build_pos_sales_by_location_insight_returns_widget_first_payload\`; browser | Automated + browser pass |
| ROUTE-02 | Staff and permission questions | Users specialist is selected and returns user-facing counts and risk data. | \`test_host_direct_staff_query_routes_to_users\`; \`test_build_permission_security_insight_returns_risk_panel\` | Automated pass (QAI-01) |
| ROUTE-03 | Product health and catalog gaps | Product Discovery is selected for read insight, without global import actions. | \`test_host_named_insight_payload_supports_users_and_product_passthrough\`; product media contract | Automated pass (QAI-01) |
| ROUTE-04 | Workspace setup from scratch | Non-import setup delegates to onboarding; it does not enter the product catalog flow. | \`test_host_direct_setup_query_routes_to_onboarding\`; \`test_host_continues_delegated_onboarding_interaction_with_same_task\` | Fixed and focused-pass |
| ROUTE-05 | Unavailable specialist | Host provides targeted unavailability guidance without rerouting to the wrong domain. | \`test_host_unavailable_selected_agent_reprompts_instead_of_misrouting\`; \`test_host_direct_sales_analysis_request_does_not_fall_back_to_capability_picker_when_pos_is_unavailable\` | Automated pass (QAI-01) |
| ROUTE-06 | Runtime restart and directory warming | Host uses concrete profile agents and control-plane fallback until Kafka discovery is current. | \`test_business_review_router_targets_concrete_specialists_even_while_the_directory_is_warming\`; \`test_explicit_delegation_uses_control_plane_while_directory_is_still_catching_up\` | Pass (prior runtime restart) |
| ROUTE-07 | Public delegation labels | Streamed status does not expose \`wa-p4-*\` runtime identifiers. | \`test_friendly_agent_label_hides_runtime_card_identifiers\`; \`test_delegation_status_uses_public_specialist_name\`; browser | Automated + browser pass |
| MEMORY-01 | Business-review follow-up | \`Which location needs the most attention from that review?\` reads the nested completed review directly. | \`test_latest_insight_follow_up_answer_uses_previous_structured_payload\`; browser | Automated + browser pass after fix |
| MEMORY-02 | Structured history preservation | Follow-up uses the prior structured artifact rather than re-parsing an unsafe text representation. | \`test_agent_preserves_direct_structured_history_over_text_only_task_history\`; \`test_conversation_websocket_rebuilds_structured_history_for_follow_up\` | Automated pass (QAI-01) |
| MEMORY-03 | Fresh topic after a review | \`I want to import products\` exits review context and starts catalog selection. | \`test_onboarding_agent_explicit_product_import_skips_saved_resume_prompt\`; browser | Automated + browser pass |
| MEMORY-04 | Closed interaction prompt | Plain text can exit a stale picker rather than being forced into an old workflow. | \`test_conversation_plain_text_breaks_out_of_closed_interaction_prompt\` | Automated pass (QAI-01) |
| MEMORY-05 | Repeated request | A repeated question returns the prior durable answer without accidental re-execution. | \`test_latest_repeated_question_response_parts\` | Automated pass (QAI-01) |
| MEMORY-06 | Clarification context formats | Host reads both serialized message parts and metadata-backed history. | \`test_latest_host_clarification_merge_reads_metadata_history_content_strings\`; \`test_latest_host_clarification_merge_reads_serialized_message_parts\` | Automated pass (QAI-01) |
| INSIGHT-01 | POS overview | Sales total, order count, AOV, payment mix, terminal notes, and top sellers are typed widgets. | \`test_build_pos_sales_overview_insight_returns_widget_first_payload\`; \`test_build_pos_top_sellers_insight_returns_ranked_widget_payload\` | Automated pass (QAI-01) |
| INSIGHT-02 | Location names | POS snapshot labels are refreshed from current inventory locations after renames. | \`test_sales_location_labels_prefer_current_inventory_name_over_pos_snapshot\`; \`test_sales_location_refresh_uses_current_search_endpoint\`; browser | Automated + browser pass |
| INSIGHT-03 | Inventory risk | Out-of-stock, low-stock, reorder, expiring products, barcode, and product media stay in inventory insight output. | \`test_build_inventory_stock_risk_insight_returns_risk_widgets\`; \`test_low_stock_summary_surfaces_out_of_stock_items\` | Automated pass (QAI-01) |
| INSIGHT-04 | Multi-domain review composition | Each completed specialist result is a typed section; host adds one final summary. | \`test_build_host_business_analyst_insight_returns_cross_service_widgets\`; \`test_business_review_drops_contradictory_empty_pos_section\`; browser | Automated + browser pass |
| INSIGHT-05 | Replenishment content | Full risk analysis appears once; replenishment is concise and non-duplicative. | \`test_business_review_reorder_section_is_compact\` | Pass (prior focused run) |
| INSIGHT-06 | Number and currency semantics | Counts remain numeric while monetary values carry their actual currency metadata. | Widget contract inspection and voice Naira test | Automated pass (QAI-01); live audio wording remains manual |
| SAFETY-01 | \`I want to import products\` | Product import defaults to the global catalog filter flow. | \`test_host_explicit_product_import_routes_directly_to_onboarding_without_listing\`; browser | Automated + browser pass |
| SAFETY-02 | Import progression | Category/brand selection, catalog results, confirmation, then import; no import before confirmation. | \`test_product_router_explicit_product_import_reuses_structured_onboarding_flow\`; \`test_namespaced_product_agent_category_selection_resumes_from_saved_workflow_state\` | Automated + browser pass before confirmation |
| SAFETY-03 | Duplicate import selection | The same selected catalog item is not imported twice. | \`test_product_import_duplicate_selection_does_not_reimport_page\` | Automated pass (QAI-01) |
| SAFETY-04 | Import failure | Authentication or MCP failures produce focused user guidance. | \`test_product_import_failure_surfaces_targeted_mcp_auth_message\` | Automated pass (QAI-01) |
| SAFETY-05 | Mutating tools | Configurations expose no destructive delete/remove actions to agents. | \`test_mcp_configs_do_not_advertise_destructive_agent_tools\` | Automated pass (QAI-01) |
| MCP-01 | Tool-to-specialist binding | Required read tools bind to the right focused specialist and host. | \`test_insight_upgrade_binds_required_read_tools_to_specialists\`; \`test_insight_upgrade_binds_required_read_tools_to_host\` | Automated pass (QAI-01) |
| MCP-02 | Tool budget | Read specialists stay within tool budgets; discovery does not own product mutation and POS insight does not own table updates. | \`test_prod_config_splits_heavy_domains_into_focused_subagents\` | Fixed and focused-pass |
| MCP-03 | Credential forwarding and runtime selection | MCP executor forwards bearer credentials and uses user/workspace runtime connection precedence. | \`test_multi_mcp_executor_routes_tools_and_forwards_bearer\`; \`test_multi_mcp_executor_prefers_user_runtime_connection_and_server_override\`; \`test_multi_mcp_executor_falls_back_to_workspace_runtime_connection\` | Automated pass (QAI-01) |
| MCP-04 | Structural location scope | Inventory and order tools receive authorized structural scope; unscoped or unauthorized multi-location calls fail safely. | \`test_multi_mcp_executor_injects_structural_scope_for_inventory_tool\`; \`test_multi_mcp_executor_rejects_terminal_inventory_call_without_structural_scope\`; \`test_multi_mcp_executor_rejects_multi_location_inventory_scope_for_non_admin\` | Automated pass (QAI-01) |
| MCP-05 | Audit permissions and workspace isolation | Audit calls require permissions and workspace mismatches are blocked. | \`test_multi_mcp_executor_enforces_audit_tool_permission_before_execution\`; \`test_multi_mcp_executor_blocks_workspace_mismatch_on_workspace_scoped_mcp_tool\` | Automated pass (QAI-01) |
| MCP-06 | Production host alignment | Versioned service configuration allows every configured MCP hostname and origin. | \`test_versioned_service_examples_allow_their_mcp_hosts\`; \`test_local_prod_envs_allow_their_mcp_hosts_when_present\` | Fixed and focused-pass |
| STREAM-01 | Optional timeouts | Client waits indefinitely when configured, honors explicit overrides, and parses terminal status updates. | \`test_call_without_timeout_waits_for_result\`; \`test_call_with_timeout_still_times_out\`; \`test_stream_message_allows_timeout_override\` | Fixed and focused-pass |
| STREAM-02 | Gateway failure semantics | Gateway includes CORS, maps permission denials to 403, emits final failed status, and does not charge on failure. | Gateway transport and billing contract tests | Automated pass (QAI-01) |
| STREAM-03 | A2A protocol integrity | RPC result/error exclusivity, Kafka envelope scope, and task principal isolation are retained. | \`test_rpc_response_requires_result_xor_error\`; \`test_kafka_envelope_roundtrip_preserves_scope_metadata\`; \`test_tenant_isolation_stores_and_enforces_task_principal\` | Automated pass (QAI-01) |
| VOICE-01 | Voice session greeting | Voice starts with a short, user-facing greeting, not internal lifecycle language. | \`test_voice_session_greeting_is_short_and_user_facing\`; \`test_voice_progress_hides_internal_agent_lifecycle_messages\` | Automated pass (QAI-01) |
| VOICE-02 | Utterance buffering | Fragmented business requests merge with time ranges; fillers and false farewell transcription are removed. | \`test_voice_transcript_batch_merges_question_with_time_range_follow_up\`; \`test_voice_transcript_batch_drops_filler_before_sales_request\`; \`test_voice_transcript_batch_drops_farewell_before_sales_request\` | Automated pass (QAI-01) |
| VOICE-03 | Voice clarification and cancellation | Voice asks for the missing range, recognizes a fresh request, and honors explicit cancellation before delegation. | \`test_voice_router_requires_time_range_for_sales_analysis\`; \`test_voice_fresh_business_request_supersedes_pending_clarification\`; \`test_voice_router_recognizes_explicit_cancellation_before_delegation\` | Automated pass (QAI-01) |
| VOICE-04 | Voice response sanitizer | Voice reads summaries, hides internal metadata, and redacts provider errors. | \`test_voice_speech_reads_structured_summary_not_widget_payload\`; \`test_voice_sanitizer_drops_submitted_task_payload_metadata\`; \`test_voice_sanitizer_maps_provider_key_error_to_safe_message\` | Automated pass (QAI-01) |
| VOICE-05 | Currency pronunciation | The Naira symbol is spoken as Naira rather than a default dollar currency. | \`test_voice_tts_expands_naira_symbol_for_unambiguous_pronunciation\` | Automated pass (QAI-01); live audio wording remains manual |

## Execution Log

### Browser Integration Runs Already Completed

1. Multi-agent business review for the last three months returned four distinct structured sections: Point of Sale, Inventory Management, User and Workspace Management, and Product Management. No raw \`TextPart(...)\` output or runtime-agent identifiers were displayed.
2. A sales clarification followed by \`The last three weeks.\` returned 43 sales totaling N2,462,960.25 in formatted widgets.
3. The saved-review follow-up \`Which location needs the most attention from that review?\` retained the three-week scope and selected Agric, Ikorodu Store from existing result data without starting a fresh today report.
4. \`I want to import products\` left the review context and opened global catalog category/brand filters without importing data.
5. Direct sales-by-location analysis returned current profile-4 location labels: Maitama, Abuja; Airport Road, Oshodi Store; and Agric, Ikorodu Store. The runtime log contained no stale location-tool warning, timeout, task failure, or registration error.

### Fixes Applied During This QA Cycle

- Non-import workspace setup now delegates to onboarding. Only an explicit product-import request enters global catalog selection.
- Minimal terminal status updates from older runtimes receive scoped fallback task/context identifiers before A2A model validation.
- Product Discovery no longer receives product-update capability, and POS Admin no longer receives POS-table update capability. Both remain available to their respective mutation specialists.
- The local production users-service environment now explicitly allows the users MCP hostname and origin.
- Contextual references such as \`from that review\` are no longer treated as a fresh time range. ISO date spans, numerical durations, and named periods remain valid date inputs. A browser regression first reproduced the wrong fresh-report route, then passed after this correction.
- Earlier cycle fixes retained: nested-review follow-up flattening, compact replenishment cards, current inventory location refresh, public specialist labels, and import-board isolation from ordinary product health insights.

## Automated Regression Command

\`\`\`text
pytest -q \\
  tests/test_langgraph_processor.py \\
  tests/test_local_tools.py \\
  tests/test_livekit_voice_worker.py \\
  tests/test_agent_filter.py \\
  tests/test_session_memory.py \\
  tests/test_agent_control_plane_store.py \\
  tests/test_agent_http_endpoints.py \\
  tests/test_client_timeout.py \\
  tests/test_insight_upgrade_contract.py \\
  tests/test_mcp_prod_alignment.py \\
  tests/test_mcp_tools.py \\
  tests/test_registry_directory.py \\
  tests/test_protocol_and_envelope.py \\
  tests/test_tenant_isolation.py \\
  tests/test_shared_runtime.py
\`\`\`

## Exit Criteria

- Every automated case in the command above passes.
- The five browser scenarios listed in the execution log pass against the signed-in workspace.
- No real catalog import, stock adjustment, purchase order, reservation, or other mutation is executed during QA.
- No raw internal lifecycle message, runtime agent ID, or unformatted model object is surfaced in a completed A2A answer.
- Any defect found in this cycle is fixed and its focused regression test is rerun before the final suite.

## Known Boundary

This report validates A2A behavior and its voice adapter contract. It does not certify unrelated visual frontend components, physical microphone/speaker hardware, or external provider account balances. Those manual checks are explicitly retained rather than incorrectly reported as automated passes.
