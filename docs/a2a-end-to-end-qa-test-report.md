# A2A System QA Test Plan and Execution Report

**Original live execution:** 2026-08-28

**Latest static revalidation:** 2026-10-18
**Latest live gateway probe:** 2026-10-18
**Environment:** Local production frontend at `http://localhost:3000` with the deployed EC2 development KA2A service. Live authenticated verification used the signed-in workspace and performed no data mutation.
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
5. `Can you analyze my business performance for the last three months?` completed the three-domain review: Point of Sale, Inventory Management, and Product Management. User-management data is excluded from this workflow; if staff activity is relevant, the host uses audit tools instead. The rendered report contained separate typed domain sections under one parent business-review container.

**Run QAI-03 (static regression and deployment contract):** 2026-10-09. The complete Kafka A2A suite completed with **468 passed** in 16.19 seconds. Lint, compilation, diff hygiene, Compose rendering, generated ECS development parameters, and Terraform validation also passed. This run added canonical analytical fact packets for evidence-first follow-ups, bounded read-MCP recovery, JEV structured-response observation, a 900-second stream budget, explicitly allow-listed browser origins and headers, Redis-backed task/context state with a seven-day context TTL, and server-owned structured artifact history ahead of stale browser widget snapshots.

**Run QAI-04 (multi-domain partial-data contract):** 2026-10-09. The complete Kafka A2A suite completed with **470 passed** in 11.58 seconds. A business review now retains independently successful section widgets while explicitly marking failed or malformed source checks as `partial` or `unavailable`; the host no longer counts those domains as complete. This is static regression evidence only and does not replace live MCP failure injection.

**Run QAI-05 (workflow-state transition integrity):** 2026-10-09. The complete Kafka A2A suite completed with **471 passed** in 11.12 seconds. Completing a targeted clarification now clears its pending workflow state durably; the final result write reloads current context so it cannot restore the stale clarification state captured earlier in the same turn. Lint, bytecode compilation, and diff hygiene passed. This remains static regression evidence; the live clarification journey is still a release gate.

**Run QAI-06 (canonical follow-up contract):** 2026-10-09. The complete Kafka A2A suite completed with **475 passed** in 15.57 seconds. Fact-backed ranking follow-ups now resolve omitted entity/metric context only when it is unambiguous, and verify selected evidence against the requested minimum, maximum, or zero direction before response text is produced. The durable-memory host path is covered. Ambiguous multi-domain context deliberately does not guess. Lint, bytecode compilation, Compose rendering, and diff hygiene passed.

**Run QAI-07 (structured response contract metadata):** 2026-10-09. The complete Kafka A2A suite completed with **476 passed** in 18.68 seconds. Every structured insight now receives an output-boundary contract result; contradictory review coverage and malformed attached evidence are observable by operators and JEV without silently rewriting the response. Lint, bytecode compilation, and diff hygiene passed.

**Run QAI-08 (response-contract enforcement):** 2026-10-09. The complete Kafka A2A suite completed with **477 passed** in 11.58 seconds. A structured review that claims complete coverage while reporting unavailable sources is downgraded to partial with low confidence and a warning before delivery. The host output path is covered. Lint, bytecode compilation, and diff hygiene passed.

**Run QAI-09 (voice failure containment):** 2026-10-09. The complete Kafka A2A suite completed with **478 passed** in 15.28 seconds. TTS provider failures are contained, and unexpected buffered-transcript processing failures produce a retry response instead of silently terminating the voice turn task. The LiveKit worker suite passed 68 tests. Lint, bytecode compilation, and diff hygiene passed.

**QAI-10 (tenant clock hardening):** 2026-10-09. The processor now uses an explicit validated workspace IANA timezone for deterministic relative windows, exposes that timezone in the insight timeframe, and safely falls back to UTC when the metadata is absent or invalid. Focused processor coverage passed; live tenant metadata propagation remains part of the live gate.

**QAI-11 (live gateway availability and browser policy):** 2026-10-10. `https://agents.dev.interaims.com/health` returned HTTP 200 with `{"status":"ok"}`. Stream preflight returned HTTP 200 with `Access-Control-Allow-Origin` for both `https://dev.interaims.com` and `https://dev.hosperator.com`, credentials enabled, and the required authorization/profile headers. An unauthenticated `POST /stream` reached FastAPI and returned the expected HTTP 422 body validation response with CORS headers. The local active browser session was Hosperator rather than Intera IMS, and the remote Intera tab redirected to sign-in, so authenticated conversational assertions remain pending rather than being marked passed.

**QAI-12 (elliptical saved-result confirmation):** 2026-10-10. The exact follow-up `to the analysis you just did` now resolves against the previous structured insight and returns a concise confirmation instead of attempting specialist delegation. Focused regression and the complete suite passed with **479 tests**.

**QAI-13 (verified workspace profile propagation):** 2026-10-10. The gateway now forwards the server-verified workspace profile as explicit non-secret request metadata and preserves it in principal claims. Delegation can therefore select the correct profile-scoped onboarding card instead of reporting `Requested agent 'Onboarding' is not registered` when the control-plane registry contains the card. Gateway/delegation tests passed; the later complete suite passed with **482 tests**.

**QAI-14 (business-review privacy boundary and MCP recovery):** 2026-10-10. Broad business-performance planning now excludes the Users specialist and user-management MCP entirely; the plan is POS, inventory, and product, with audit tools available at the host boundary for staff/user activity when needed. Regression coverage passed with **482 tests**. The live audit and notification MCP images were repaired to install their required runtime `greenlet` dependency, their host allowlists remained configured, and public MCP stream probes now return HTTP 200 rather than the previous 421/502 startup failures. Affected upstream services and A2A were redeployed, and Caddy was refreshed after container recreation.

**QAI-16 (business-review inventory deduplication):** 2026-10-10. Cross-domain business reviews now emit one canonical inventory risk table containing risk, product, barcode, location, available quantity, minimum stock, and reorder point. Repeated priority/risk/replenishment product lists are removed from the review composition, and the separate replenishment count is suppressed when its threshold semantics conflict with the canonical stock-risk posture. A fresh signed-in profile-4 browser run passed after redeploy: two out-of-stock products were shown once, both at Maitama, while the POS section still showed all three configured locations. The complete suite passed with **483 tests**.

**QAI-17 (deployed A2A and structured value semantics):** 2026-10-18. The complete Kafka A2A suite passed with **491 tests in 11.23 seconds**; the processor regression subset passed with **183 tests**. The A2A service was rebuilt and redeployed with all 21 development containers healthy. Live browser checks passed for sales clarification and follow-up, current location leadership, canonical stock-risk output with barcode/media fields, POS troubleshooting, import safety, onboarding setup guidance, and the three-domain one-year business review. Audit search was narrowed from unrelated workspace events to the named subject, then further constrained to reorder-point events so inventory availability updates cannot be presented as the answer. The frontend value contract now distinguishes counts from money, preventing source-service counts from rendering as Naira.

**QAI-18 (warehouse-form routing):** 2026-10-18. The exact demo question `Can you prepare the form for adding my first warehouse?` initially reproduced a defect: host onboarding routed it to `Choose Catalog Filters` and the product-import flow. The cause was two routing precedence gaps: explicit inventory setup was evaluated after generic onboarding, and the inventory router did not classify warehouse/form language as `inventory_setup`. The fix routes explicit setup actions to Inventory and selects the inventory-setup specialist. The focused processor suite passed **183 tests**, the A2A service was rebuilt and redeployed, public health returned `{"status":"ok"}`, and the authenticated browser retest rendered `Dynamic Form → Create Stock Location` with location name, location type, parent location, and structural-location controls. QA stopped before `Submit Form`; no warehouse was created.

**QAI-19 (shared dynamic-form selects):** 2026-10-18. The live warehouse form exposed native select controls with unreadable white styling in the dark interface. The frontend dynamic-form renderer now reuses the existing `ReactSelectField` component for all select fields, including portal positioning, shared theme tokens, and accessible combo-box semantics. After `yarn build` and a production-server restart, the authenticated browser retest showed `Location Type` and `Parent Location` as shared React Select controls. No form submission was performed.

**QAI-20 (widget select interaction):** 2026-10-18. The first React Select implementation still closed the AI widget because its portaled menu and control were treated as outside clicks. The widget outside-click boundary now protects the shared React Select field and its portaled menu/options. After a production build and restart, the browser retest kept the widget open while opening `Location Type`, selecting `Aisle`, opening `Parent Location`, and selecting `Agric, Ikorodu Store`. No warehouse was submitted.

**QAI-21 (child-location continuation routing):** 2026-10-18. After the warehouse was created, the user supplied a complete plain-text request for 20 child bins under `Oshodi Isolo`, including the naming pattern, non-structural flag, auto-generated codes, capacity, and preview-only instruction. The live runtime incorrectly asked `What time range should I use for the inventory analysis?` because the host treated the continuation as a fresh inventory-analysis request. The fix detects complete child-location setup replies before generic inventory clarification and routes them to `inventory_setup`. The focused processor suite passed **185 tests**, the A2A service was rebuilt and redeployed, public health returned `{"status":"ok"}`, and the authenticated browser retest returned a preview of all 20 requested locations. No child locations were created.

**QAI-22 (procurement versus marketplace routing):** 2026-10-18. The exact demo follow-up `Create a draft purchase order grouped by supplier.` was incorrectly routed to the product marketplace specialist and opened eBay. The root cause was the generic `supplier` marketplace heuristic running before purchase-order intent. The fix gives draft/create purchase-order language precedence and routes it to `inventory_procurement`; supplier replenishment-performance questions receive the same protection. The focused processor subset passed **15 tests**, the A2A service was rebuilt and redeployed, public health returned `{"status":"ok"}`, and authenticated browser retests stayed inside inventory procurement. The contextual reorder follow-up returned a supplier-grouped draft planning response and requested confirmation before any write. No purchase order was created. The supplier-performance run completed after confirmation and returned Satnam Investment Nigeria Ltd as the leading delayed supplier. A separate open defect remains: `get_purchase_order_analytics` returned a schema validation error for missing `supplier__id`, after which the agent recovered with receiving-exception and audit data; the resulting `insight_response` also needs a typed widget contract instead of raw serialized text.

**QAI-23 (serialized insight widget compatibility):** 2026-10-18. Supplier delay analysis exposed three backend response shapes that were reaching the browser as raw YAML-like text or JSON code: `comparison/timeline/risk`, `top_offenders/frequency_insight/recommended_next_steps`, and JSON widgets without a `kind` discriminator. The frontend structured-output boundary now normalizes these variants into the existing `comparison_table`, `timeline`, `risk_panel`, `progress_tracker`, `text_block`, and `text_list` renderers. The parser fixture passed and the production frontend build passed. The live retest also confirmed the remaining backend limitation: one execution asked for procurement data but then returned that the inventory setup tools lacked PO analytics access, so backend capability/routing still requires follow-up; the renderer no longer needs to expose that result as JSON when a typed insight payload is received.

**Current live gate:** the public gateway is available and CORS is now verified for both supported frontends. The authenticated chat/voice matrix still requires an active signed-in Intera IMS session: sales clarification and minimum-location follow-up, new-topic global import, multi-domain review, fragmented voice request, cancellation, and a tenant-local `today` query. No authenticated request or mutation was made during this probe.

The deployed gateway preflight probe was also attempted on 2026-10-09 for both supported deployment origins; both requests timed out before receiving HTTP headers. Deployment availability must be restored before interpreting browser CORS behavior.

Read-only AWS verification for the configured `us-east-2` account found no ECS clusters and no load balancers, although both public A2A names still resolve to the edge address. The public gateway is responding through the edge, but the underlying deployment target remains outside the inspected account/region.

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
| ROUTE-08 | Warehouse form routing | Explicit warehouse setup reaches Inventory Setup and does not open product catalog filters. | \`test_generic_inventory_setup_language_prefers_onboarding_over_direct_mutation\`; authenticated browser | Automated + browser pass (QAI-18) |
| UI-01 | Dynamic form controls | Select fields reuse the shared React Select component and remain readable in the branded dark interface. | `npx eslint components/interaction-handlers.tsx`; `yarn build`; authenticated browser | Automated + browser pass (QAI-19) |
| UI-02 | Widget select interaction | Portaled React Select controls do not trigger the AI widget outside-click close handler. | `npx eslint components/agents/ai-chat-widget.tsx`; `yarn build`; authenticated browser | Automated + browser pass (QAI-20) |
| MEMORY-01 | Business-review follow-up | \`Which location needs the most attention from that review?\` reads the nested completed review directly. | \`test_latest_insight_follow_up_answer_uses_previous_structured_payload\`; browser | Automated + browser pass after fix |
| MEMORY-02 | Structured history preservation | Follow-up uses the server-persisted structured artifact when available; direct browser history is retained only for legacy text-only tasks. | \`test_agent_prefers_server_structured_artifact_over_direct_history\`; \`test_agent_preserves_direct_structured_history_over_text_only_task_history\`; \`test_conversation_websocket_rebuilds_structured_history_for_follow_up\` | Automated pass (QAI-03) |
| MEMORY-03 | Fresh topic after a review | \`I want to import products\` exits review context and starts catalog selection. | \`test_onboarding_agent_explicit_product_import_skips_saved_resume_prompt\`; browser | Automated + browser pass |
| MEMORY-04 | Closed interaction prompt | Plain text can exit a stale picker rather than being forced into an old workflow. | \`test_conversation_plain_text_breaks_out_of_closed_interaction_prompt\` | Automated pass (QAI-01) |
| MEMORY-05 | Repeated request | A repeated question returns the prior durable answer without accidental re-execution. | \`test_latest_repeated_question_response_parts\` | Automated pass (QAI-01) |
| MEMORY-06 | Clarification context formats | Host reads both serialized message parts and metadata-backed history. | \`test_latest_host_clarification_merge_reads_metadata_history_content_strings\`; \`test_latest_host_clarification_merge_reads_serialized_message_parts\` | Automated pass (QAI-01) |
| MEMORY-07 | Clarification completion integrity | Completing a clarification does not resurrect the pending workflow state while persisting the final analysis. | \`test_completed_clarification_does_not_resurrect_stale_workflow_state\` | Automated pass (QAI-05) |
| MEMORY-08 | Durable abbreviated follow-up | An unambiguous saved fact packet resolves a shortened ranking follow-up after context restoration; mixed-domain evidence does not guess. | \`test_latest_insight_follow_up_uses_durable_fact_packet_for_elliptical_ranking\`; \`test_fact_packet_does_not_guess_an_elliptical_follow_up_with_multiple_dimensions\` | Automated pass (QAI-06) |
| INSIGHT-01 | POS overview | Sales total, order count, AOV, payment mix, terminal notes, and top sellers are typed widgets. | \`test_build_pos_sales_overview_insight_returns_widget_first_payload\`; \`test_build_pos_top_sellers_insight_returns_ranked_widget_payload\` | Automated pass (QAI-01) |
| INSIGHT-02 | Location names | POS snapshot labels are refreshed from current inventory locations after renames. | \`test_sales_location_labels_prefer_current_inventory_name_over_pos_snapshot\`; \`test_sales_location_refresh_uses_current_search_endpoint\`; browser | Automated + browser pass |
| INSIGHT-03 | Inventory risk | Out-of-stock, low-stock, reorder, expiring products, barcode, and product media stay in inventory insight output. | \`test_build_inventory_stock_risk_insight_returns_risk_widgets\`; \`test_low_stock_summary_surfaces_out_of_stock_items\` | Automated pass (QAI-01) |
| INSIGHT-04 | Multi-domain review composition | Each completed specialist result is a typed section; host adds one final summary. | \`test_build_host_business_analyst_insight_returns_cross_service_widgets\`; \`test_business_review_drops_contradictory_empty_pos_section\`; browser | Automated + browser pass |
| INSIGHT-07 | Partial-source truthfulness | A review section keeps usable sibling results but exposes unavailable checks and the final summary does not claim full domain coverage. | \`test_business_review_marks_unavailable_source_checks_without_hiding_available_data\`; \`test_business_review_summary_does_not_claim_complete_coverage_for_partial_domain\` | Automated pass (QAI-04) |
| INSIGHT-08 | Ranking contract | A fact-backed follow-up selects only rows satisfying the requested direction and rejects the opposite ranking. | \`test_fact_packet_resolves_unambiguous_elliptical_follow_up_from_prior_frame\`; \`test_analytic_selection_rejects_the_opposite_ranking_direction\` | Automated pass (QAI-06) |
| INSIGHT-09 | Structured response contract | Completed insight parts expose fact and response-contract validation metadata, including contradictory review coverage. | \`test_insight_response_contract_rejects_contradictory_review_coverage\`; existing output metadata assertions | Automated pass (QAI-07) |
| INSIGHT-10 | Response-contract enforcement | Contradictory complete coverage is downgraded to partial before the structured result is delivered. | \`test_insight_response_contract_downgrades_contradictory_review_coverage\`; host output assertion | Automated pass (QAI-08) |
| INSIGHT-11 | Tenant-local date boundary | Explicit workspace IANA timezone controls relative date windows; malformed or absent timezone metadata falls back to UTC. | \`test_workspace_timezone_is_validated_and_used_for_relative_windows\` | Automated pass (QAI-10); live metadata propagation pending |
| VOICE-07 | Voice failure containment | TTS/provider failures do not terminate the voice turn silently; buffered transcript failures have a user-facing retry fallback. | \`test_safe_voice_playback_contains_provider_failure\`; LiveKit worker suite | Automated pass (QAI-09); live audio still manual |
| INSIGHT-05 | Replenishment content | Full risk analysis appears once; replenishment is concise and non-duplicative. | \`test_business_review_reorder_section_is_compact\`; \`test_business_review_stock_risk_uses_one_canonical_product_table\` | Automated pass (QAI-16) |
| INSIGHT-06 | Number and currency semantics | Counts remain numeric while monetary values carry their actual currency metadata. | Widget contract inspection and voice Naira test | Automated pass (QAI-01); live audio wording remains manual |
| INSIGHT-07 | Explicit value semantics | Structured widgets declare count versus money values; the frontend does not infer currency for an unlabelled count. | `value_kind` contract and production frontend build | Automated and bundle pass (QAI-17) |
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

### Production Deployment Verification (2026-10-10)

1. The repository deployment wrapper was inspected and executed with `./infra/scripts/deploy_ec2_dev_service.sh inventory a2a`. It deployed only `kafka_a2a`, rebuilt the image, and recreated the A2A gateway, proxy, runtime, specialist agents, and voice agent on `intera-dev-server`.
2. The first post-deploy public check returned HTTP 502 because the shared Caddy reverse proxy retained the previous gateway container address after recreation. The A2A containers were running and the gateway responded successfully from inside its container.
3. Restarting the shared Caddy container refreshed service discovery. Public `https://agents.dev.interaims.com/health` now returns HTTP 200, `OPTIONS /stream` returns HTTP 200 with the expected `dev.interaims.com` CORS headers, and an unauthenticated `POST /stream` reaches FastAPI and returns its expected validation response rather than an edge 502.
4. The audit and notification MCP images initially restarted with `ImportError: ... greenlet`; moving `greenlet` to runtime dependencies and rebuilding both services restored their public MCP stream endpoints to HTTP 200. No 421 `Invalid Host header` response remains on the configured audit, notification, or subscription MCP hostnames.
5. The signed-in profile-4 workspace completed the multi-domain review with separate POS, Inventory Management, and Product Management sections, current location names, Naira values, product images, and barcodes, without exposing the Users specialist.

### Live authenticated verification after the 2026-10-10 redeploy (QAI-15)

1. The fresh follow-up `Which branch had the least revenue in that review?` returned `Agric, Ikorodu Store` at `₦3,260,500.25`, correctly selecting the minimum from the POS location table. A defect was found and fixed during this run: inventory scope metric grids had been persisted as false location-sales facts, and legacy packets could therefore answer with labels such as `Needs Reorder`. New extraction excludes metric grids, and answer-time filtering protects old packets.
2. The fresh topic switch `I want to import products from the global catalog.` exited the saved review and opened the global catalog category/brand selection flow without performing an import.
3. The browser voice call reached `Connected`, produced the short user-facing greeting, exposed both microphone and assistant-audio mute controls, and ended cleanly. The voice logs confirmed profile-4 authentication, host resolution, LiveKit session start, transcript observation, and client-initiated shutdown with no runtime errors.
4. After the fix, the focused analytics regression set passed 13 tests and the deployed A2A containers remained healthy. Physical speaker/microphone output remains a manual demo-device check.

### Live authenticated verification after the inventory deduplication redeploy (QAI-16)

1. A clean browser session ran `Now I want you to help me analyze my business data for the past one year.` and completed the review without the generic agent-service fallback.
2. Inventory Management rendered one `Stock risks by product and location` table with two rows: Mama Gold Rice 5kg and Colgate Triple Action Toothpaste 100ml, both at Maitama, Abuja, with barcodes and stock thresholds.
3. The repeated `Highest priority stock risks`, `Items needing attention`, and `Products to replenish` product lists were absent. The separate replenishment count was also absent, removing the observed `Needs reorder: 0` versus `Products to replenish: 2` contradiction.
4. POS location output continued to show all three configured locations: Maitama, Abuja; Airport Road, Oshodi Store; and Agric, Ikorodu Store. This confirms that two affected inventory rows do not imply only two configured locations.

### Live authenticated verification after the warehouse-routing redeploy (QAI-18)

1. The exact question `Can you prepare the form for adding my first warehouse?` first reproduced the defect by opening the product-import `Choose Catalog Filters` picker.
2. Explicit inventory setup routing was corrected, and the inventory router was updated to select `inventory_setup` for warehouse/form language before inventory visibility.
3. After redeployment, the same question streamed `HOST → INVENTORY` delegation and rendered `Dynamic Form → Create Stock Location` with location name, location type, parent location, and structural-location controls.
4. The test stopped at the confirmation-gated form. No warehouse or other mutation was submitted.

### Live authenticated verification after the dynamic-form select correction (QAI-19)

1. The warehouse form was reopened after the frontend production build and restart.
2. `Location Type` and `Parent Location` rendered as shared React Select combo boxes rather than native white selects.
3. The form remained confirmation-gated; no warehouse was created.

### Live authenticated verification after the widget select-boundary correction (QAI-20)

1. The first interaction reproduced the defect: clicking the location-type control closed the AI widget.
2. The outside-click handler was updated to protect the React Select field, portaled menu, and options.
3. After the production restart, `Location Type` opened and accepted `Aisle` without closing the widget; `Parent Location` opened and accepted `Agric, Ikorodu Store` the same way.
4. The form remained confirmation-gated and no warehouse was created.

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
- The current live acceptance matrix is rerun after the QAI-03 deployment-state changes; QAI-15 and QAI-16 record the authenticated profile-4 browser results.
- QAI-11 confirms public gateway health and CORS for `dev.interaims.com` and `dev.hosperator.com`; it does not replace authenticated chat/voice acceptance.
- Production deployment verification confirms `inventory a2a` redeploys the A2A stack and that Caddy must be refreshed after container recreation when its upstream address is stale.
- No real catalog import, stock adjustment, purchase order, reservation, or other mutation is executed during QA.
- No raw internal lifecycle message, runtime agent ID, or unformatted model object is surfaced in a completed A2A answer.
- QAI-15 confirms the profile-4 multi-domain review, factual lowest-sales follow-up, global import topic switch, and voice connection lifecycle after the final deployment.
- QAI-16 confirms the profile-4 inventory-risk deduplication, canonical product/location table, and three-location POS scope after the final redeployment.
- QAI-18 confirms the exact first-warehouse demo question reaches the inventory setup form rather than product import, after the final A2A redeployment.
- QAI-19 confirms dynamic-form select fields reuse the shared branded React Select component after the frontend production restart.
- QAI-20 confirms portaled dynamic-form selects no longer close the AI widget during selection.
- QAI-21 confirms a complete plain-text child-location setup reply continues the pending inventory setup workflow instead of asking an unrelated inventory-analysis time-range question.
- Any defect found in this cycle is fixed and its focused regression test is rerun before the final suite.

### QAI-24 Demo-question retest (2026-10-18/19)

The ten landing-page demo scenarios were retested against the authenticated profile-4 workspace. All 20 published prompts were exercised directly; sales, onboarding, procurement, POS, and audit follow-ups were also run in shared conversation context. Active read flows returned without the generic agent-service fallback, warehouse setup returned a confirmation-gated `Create Stock Location` form, sales follow-ups preserved the selected period, supplier delay without a period asked only for the missing time frame, and role-control routing recovered after the users MCP ID-contract fix.

The users MCP fix normalizes numeric role/group database IDs to the string identifiers required by `CompanyRolesResponsePayload`. The users service was redeployed with `./infra/scripts/deploy_ec2_dev_service.sh inventory users`; A2A, inventory MCP, and users MCP health checks returned `{"status":"ok"}`. The focused A2A regression set passed 66 tests. The frontend production build passed, the RTK endpoint audit passed, and the agent export/PDF suite passed all 12 tests.

The frontend structured-output parser was also widened to recognize the current YAML-like `insight_response` envelope when `widgets:` is not prefixed by the legacy list marker. A live morning-brief retest no longer displayed the raw envelope. The current workspace still returns the morning briefing as readable text rather than the full widget layout, and the draft purchase-order prompt asks for missing supplier/line details in readable text; these are presentation/completeness gaps, not service failures. The two anti-theft prompts remain excluded from the green acceptance count because the demo labels that scenario `Coming soon`.

**QAI-24 result:** active routing and safety behavior passed; full widget parity for every demo prompt is not yet green. No warehouse, purchase order, notification, or other mutation was submitted.

### QAI-25 Same-conversation demo sweep (2026-10-19)

The demo prompts were continued in one persistent authenticated browser conversation without refreshing the page or starting a new chat. The warehouse form was cancelled in-place after the first run exposed a state bug: the original cancellation opened the product-import picker and the next stop request returned the generic agent error. An early cancellation guard was added to clear saved workflow state before domain routing; processor coverage passed 190 tests, A2A was redeployed, and a subsequent cancellation returned a normal confirmation.

The same conversation then produced readable widgets for reorder candidates, POS visibility, terminal activity, audit search, and permission activity. The supplier-grouped purchase-order request correctly remained confirmation-gated and did not create a PO. The long-running morning briefing completed after waiting rather than timing out, but returned a large plain-text response instead of the expected widget stack. The anti-theft demo prompt and supplier-delay prompt returned the generic agent-service fallback in this long-lived context; the anti-theft follow-up eventually returned plain text. A backend capability guard and narrow frontend fallback were added for the documented Coming Soon anti-theft prompt, but the live browser retest still reproduced the generic fallback, so these fixes are not marked as verified.

**QAI-25 result:** same-conversation persistence and cancellation are improved, but the full demo sweep is not green. Remaining blockers are supplier-delay routing in the long-lived context, anti-theft fallback handling, and widget parity for the morning briefing and anti-theft follow-up. No mutation was submitted.

### QAI-31 Operational-problem backlog closure (2026-10-19)

The documented 29-problem inventory-operations backlog was revalidated against the current source and regression suites. The final implementation slice added a POS visibility diagnostic table that separates catalog match, variant, POS visibility, sellable price, location stock, catalog sync, and cashier-permission evidence after the user supplies a product name, SKU, or barcode. Agent setup now leaves its setup-only loading state after 20 seconds and offers explicit retry/page-refresh actions; this does not change long-running A2A request timeouts.

Evidence: the complete Kafka A2A suite passed **510 tests**, the focused processor/contract slice passed **217 tests**, frontend typecheck passed, the export/PDF suite passed **12 tests**, structured-output parser tests passed **2 tests**, the RTK endpoint audit passed, and the production frontend build passed. The A2A service was rebuilt and redeployed with `inventory a2a`; after normal gateway warm-up, `https://agents.dev.interaims.com/health` returned `{"status":"ok"}`. The 29-item backlog records every item as verified by automated evidence. Authenticated browser click-through could not be repeated in this Codex session because local browser automation is disabled by the current Work settings; no mutation was submitted.

### QAI-32 Hosted frontend deployment boundary (2026-10-19)

The frontend agent-rendering slice was committed as `4837d8e` and the chat
reset guard was committed as `1710c90`, both on the `intera_f` `dev` branch.
The local production build and typecheck pass, and the A2A service remains
healthy on the EC2 development host. The current AWS account has no matching
Intera IMS Amplify application, deployment workflow, or CloudFront
distribution for `dev.interaims.com`; its `amplify list-apps` results contain
only unrelated applications. The hosted domain itself responds as a
CloudFront-served Next.js application, so the deployment owner/account is
outside the configured AWS context.

**QAI-32 result:** the code is pushed and locally verified, but hosted
frontend deployment and authenticated browser acceptance remain pending until
the correct hosting account or deployment pipeline is available. This is an
infrastructure boundary, not a frontend test pass.

### QAI-26 Demo follow-up correction and redeploy (2026-10-19)

1. The cashier-role demo follow-up `Notify the staff member when the role is updated.` was reproduced as an unrelated staff-activity response. The root cause was missing workflow precedence for role notification language.
2. The A2A processor now keeps cashier role review and role notification in a structured, confirmation-gated permissions workflow. It returns a capability comparison widget plus a notification-preview form; it does not send or apply a role change without explicit confirmation.
3. The Coming Soon suspicious-stock-movement prompt now returns a structured read-only investigation form and checklist instead of plain text or the generic service fallback. The live browser verification is still pending because the local browser session lost its authenticated workspace after the redeploy and now shows the company-profile onboarding screen.
4. Focused processor coverage passed `191 tests`. The A2A service was rebuilt and redeployed with `./infra/scripts/deploy_ec2_dev_service.sh inventory a2a`; after restarting Caddy, public health returned `{"status":"ok"}`.

5. A subsequent browser retest exposed the same stale-workflow gate on supplier-delay clarification. That gate was removed for period-sensitive supplier requests, the focused processor suite passed `192 tests`, and the A2A service was redeployed again. Browser confirmation of the missing-period and 28-day supplier cases is pending restoration of the authenticated local session.

6. The complete A2A test suite then passed `502 tests in 11.60 seconds`. A final public health check returned `{"status":"ok"}` after the transient restart window.

7. The morning briefing still exposed a serialized specialist payload in a mixed delegated response. The delegated-response boundary now removes that transport suffix from widget text before publication. Processor coverage passed `194 tests`, the frontend production build passed, and A2A was redeployed again; public health returned `{"status":"ok"}`. A final authenticated browser confirmation of the sanitized card remains to be captured.

**QAI-26 result:** backend contracts were green and deployed; the remaining authenticated browser checks were completed in QAI-27 below.

### QAI-27 Final post-deploy acceptance (2026-10-19)

The final A2A redeploy included the delegated-response sanitizer and typed promotion for serialized morning-briefing JSON. In the authenticated browser, a fresh `Give me a morning briefing before I open the stores.` request now renders separate `PO Pipeline (snapshot)`, `Recent PO activity`, `Open receiving exceptions`, and `Top procurement risks` widgets. The response contains no raw `insight_response:` marker and no JSON code block.

The same live session also verified the remaining QAI-26 cases: missing supplier-delay timeframe asks only for the timeframe; `Past 28 days` produces a typed supplier-performance result; cashier-role review produces a capability comparison and confirmation-gated notification form; and suspicious stock movement produces a structured read-only investigation form and checklist. No mutation was submitted.

Focused processor coverage passed `194` tests before the final promotion change; the authoritative complete A2A suite then passed `504 tests in 12.27 seconds`. `yarn build` passed with the RTK endpoint audit reporting `90 unique endpoints across 54 files`. Public `https://agents.dev.interaims.com/health` returned `{"status":"ok"}` after the final A2A redeploy and Caddy refresh.

**QAI-27 result:** the Sprint 5 A2A/demo acceptance gate is green for the tested demo flows. The remaining items below are future product improvements, not unresolved defects in this acceptance run.

### QAI-28 Same-chat 20-question verification and follow-up precedence (2026-10-19)

The twenty published demo prompts were exercised in one persistent authenticated browser conversation. The run preserved the conversation and did not intentionally refresh or open a second chat. Read-only sales, inventory, POS, audit, procurement, onboarding, and role-review paths returned responses; the supplier-delay prompt asked for a missing timeframe and then produced a typed 28-day result; and mutating prompts remained confirmation-gated.

The retest exposed two remaining defects. The review-order, morning-briefing, and priority-action prompts can still fall back to long prose when the live runtime uses an older workflow path instead of the current typed demo contract. More importantly, the frontend history formatter classified `Notify the staff member when the role is updated.` as a generic staff-activity question and generated the wrong prose even after the role-review widget had already supplied the correct notification form. The frontend precedence fix now preserves that form; its local endpoint/export checks pass, but the deployed web bundle must be rebuilt and relaunched before this fix can be claimed live.

The A2A suite passes `504 tests`. Frontend RTK endpoint and export/PDF tests pass (`90 unique endpoints across 54 files`, `13 tests`), and frontend typecheck/build pass after the narrow LiveKit `RoomConfiguration` compatibility correction. The fix is committed as `e726c41` on `intera_f` branch `dev`; GitHub CI passed. The local production app is running on port 3000. The hosted `dev.interaims.com` bundle still has the previous ETag, and no deployment workflow or matching hosted app is available in the configured AWS account, so hosted verification remains pending.

**QAI-28 result:** the one-chat 20-question acceptance gate is not yet green. The remaining work is to rebuild/relaunch the frontend, retest the four affected prompts in the same conversation, and update this report only after their rendered widgets are verified.

### QAI-29 Final deployed one-chat demo sweep (2026-10-19)

The A2A service was rebuilt and redeployed with `./infra/scripts/deploy_ec2_dev_service.sh inventory a2a`. All deployed A2A containers started successfully, the public health endpoint returned `{"status":"ok"}`, and the complete backend suite passed `505 tests in 10.80 seconds`.

The local production frontend was rebuilt with `yarn build` and restarted on `http://localhost:3000`. The RTK endpoint audit passed (`90 unique endpoints across 54 files`) and frontend typecheck passed. The chat was authenticated and the following prompts were run in one persistent conversation after the chat reset completed:

| # | Demo prompt area | Rendered result | Status |
|---:|---|---|---|
| 1 | Sales today | `Today across locations` widget with sales, orders, average basket, and location scope | PASS |
| 2 | Leading location | One-year location-sales widget; `Maitama, Abuja` identified as leader | PASS |
| 3 | Out-of-stock products | Stock-risk summary and product/location table with barcodes | PASS |
| 4 | Best sellers, seven days | Seven-day sales widget and top-sellers section | PASS |
| 5 | Workspace setup | Recommended setup-path widget | PASS |
| 6 | First warehouse | Confirmation-gated `Create Stock Location` form | PASS |
| 7 | Reorder recommendations | Stock-risk/reorder widget | PASS |
| 8 | Draft PO by supplier | Confirmation-gated supplier-grouped draft form | PASS |
| 9 | POS product visibility | Product lookup table and next POS checks | PASS |
| 10 | Top terminal today | Terminal/cashier activity widget | PASS |
| 11 | Reorder-point audit | Audit-search widget, including an empty result state | PASS |
| 12 | Risky permission changes | Security-activity widget, including an empty result state | PASS |
| 13 | Suspicious stock movement | Read-only investigation form and checklist | PASS |
| 14 | First review action | Review-order ranked-list widget | PASS |
| 15 | Morning briefing | Priority actions, operational snapshot, risks, and sales-by-location widgets | PASS |
| 16 | First action to take | Priority-actions ranked-list widget | PASS |
| 17 | Supplier delay without timeframe | Timeframe clarification, with no unnecessary widgets | PASS |
| 18 | Alternative supplier action | Supplier-action form | PASS |
| 19 | Cashier role capabilities | Capability comparison and notification-preview form | PASS |
| 20 | Notify staff after role update | Confirmation-gated notification form | PASS |

The first automated sweep also exposed a browser-only race when a prompt was submitted immediately while `Start new chat` was clearing the previous session. The frontend now disables submission during that reset transition; the rebuilt local bundle passes typecheck and production build. A properly initialized fresh conversation was then used for the authoritative run above. No raw JSON, transport `TextPart(...)`, generic agent-service fallback, or mutation was accepted as a passing answer. No warehouse, purchase order, role change, or notification was submitted.

**QAI-29 result:** `20/20` demo questions passed in one authenticated conversation. The Sprint 5 demo-question acceptance gate is green for the current deployed A2A service. The earlier QAI-28 status is superseded by this post-deploy verification.

### Current Problems Identified Before the Next 29-Problem Iteration

### QAI-30 Initial 29-problem iteration (2026-10-19)

The 29 operational problems are now tracked in
[`a2a-29-operational-problem-backlog.md`](a2a-29-operational-problem-backlog.md)
with acceptance criteria and ownership areas. The first implementation slice
normalizes exact duplicate insight widgets at the A2A response boundary and
records a warning instead of silently hiding the composition defect. The
frontend also suppresses an identical structured insight repeated within the
same task, while preserving distinct widgets and turns. The generic frontend
numeric formatter no longer treats a field named only `value` as currency;
money now requires an explicit format or a monetary label such as revenue,
price, cost, or sales.

Focused backend verification passed **213 tests** before deployment and the
full A2A suite passed **506 tests**. The A2A service was rebuilt and deployed
with `infra/scripts/deploy_ec2_dev_service.sh inventory a2a`; all containers
were running and the public health endpoint recovered to `{"status":"ok"}`
after the normal rollout warm-up. Frontend typecheck, production build,
endpoint audit, and **12 export tests** passed. Browser automation remains
unavailable in this session, so browser-level verification of this slice is
still explicitly pending.

These are the existing demo-scope problems to resolve first:

1. **POS product visibility:** the original answer gave a checklist but did not ask for a product name, SKU/barcode, or terminal/location. The deployed diagnostic now collects those details and performs a product lookup. A live barcode test returned a product-specific diagnosis and next actions when no catalog match was found.
2. **Supplier-delay clarification:** resolved and verified in the final authenticated browser run.
3. **Anti-theft browser verification:** resolved and verified as a structured read-only investigation widget.
4. **Anti-theft follow-up behavior:** the initial investigation flow is verified; richer follow-up actions remain a future enhancement.
5. **Morning briefing rendering:** resolved and verified as separate typed widgets without raw serialized payloads.
6. **Draft purchase-order presentation:** the flow correctly avoids an unsafe write and asks for confirmation, but the clarification is a long text block rather than a concise structured supplier-grouped draft form.
7. **Role and notification browser verification:** the backend now returns a permissions comparison and confirmation-gated notification form, but the browser must verify both prompts after authentication is restored.
8. **Same-conversation workflow cleanup:** cancellation and topic-switch behavior are covered by the completed acceptance run; broader demo expansion remains future work.

No new 29-problem backlog work should begin until these demo-scope problems are green in one persistent conversation.

## Known Boundary

This report validates A2A behavior and its voice adapter contract. It does not certify unrelated visual frontend components, physical microphone/speaker hardware, or external provider account balances. Those manual checks are explicitly retained rather than incorrectly reported as automated passes.
