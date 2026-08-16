# Grafana AI Agent — Interactive Dashboard Management Architecture

**AI PROPOSES → HUMAN REVIEWS → HUMAN APPROVES → SYSTEM BUILDS → VALIDATES → MCP EXECUTES → GRAFANA RENDERS**

---

## 1. Design Goals


- Enforce a hard human-approval boundary before any CREATE, UPDATE, or REMOVE reaches Grafana.
- Keep metric discovery, target resolution, query generation, and JSON compilation deterministic and backend-owned, not LLM-owned.
- Support incremental, panel-level conversational edits without regenerating unrelated panels.
- Support multiple visualization types with isolated, per-type configuration.
- Define an intermediate representation (IR) that is the single source of truth between proposal and Grafana JSON.
- Keep the feature independently demonstrable without depending on the external chart-system integration another team member owns.
- Identify a 4-day P0 slice that proves the full loop end-to-end on one flow.

## 2. Overall Architecture


```
User
 │  natural language
 ▼
AI Agent (LLM + ADK orchestration)
 │  intent → discovery calls → proposal assembly
 ▼
Observability Wrapper/Backend
 │  metric/target/query resolution, IR compilation, validation
 ▼
=====================================================
   HUMAN APPROVAL REQUIRED HERE (conversational UI)
=====================================================
 │  approved IR only
 ▼
Dashboard Builder (backend)
 │  deterministic Grafana JSON generation
 ▼
Validation + Policy/Safety Checks
 ▼
Grafana MCP
 ▼
Grafana
 ▼
Interactive Dashboard (Grafana-native UI)
```

The wrapper sits between the AI agent and Grafana MCP at every stage — during discovery (Stage 1) and during the write (Stage 2). The AI agent never calls Grafana MCP directly; it calls wrapper tools, and only the wrapper (after approval) calls MCP.

---

## 3. Two-Stage Interaction Model


| | Stage 1 — Proposal | Stage 2 — Build |
|---|---|---|
| Trigger | Any user request referencing a dashboard change | Explicit user approval of a specific proposal version |
| Owner | AI agent + wrapper (read-only/discovery calls) | Wrapper (deterministic compiler) |
| Output | Proposed IR, shown conversationally | Grafana JSON, written via MCP |
| Mutates Grafana? | Never | Only after validation + policy pass |
| Reversible mid-flow? | Yes, freely, via conversation | No — this is the point of no return |

The two stages are separated by a single object: the **approved IR**. Stage 1 produces candidate IR versions; Stage 2 consumes exactly one, frozen, approved IR version and nothing else.

---

## 4. Stage 1 — Conversational Proposal


### Sequence

```
User request
 → AI: intent extraction (create/update/remove, target dashboard, target panels)
 → Wrapper: metric discovery       (read-only)
 → Wrapper: target/node resolution (read-only)
 → Wrapper: query generation       (read-only — produces candidate PromQL/query, does not execute a write)
 → AI: panel specification (title, metric, query, requested/inferred visualization)
 → AI: visualization specification (type + minimal config)
 → AI: dashboard-variable specification (if requested/implied)
 → Wrapper: IR assembly + IR-level validation (structural only, no MCP write)
 → Interactive response rendered to user (proposal card + query text)
```

### What the AI generates
- Natural-language → structured intent (operation type, target dashboard/panel references, requested metrics, requested visualization, requested variables).
- Panel titles and dashboard naming when not specified by the user.
- Selection of a visualization type **only** when the user did not specify one (see §6).
- Draft visualization configuration defaults (units, thresholds) subject to user override.

### What is deterministic backend output (not LLM-authored)
- Whether a metric exists and its exact name (metric discovery).
- Whether a target/node/instance exists and its resolved identifier.
- The actual query string syntax and label matchers (query generation), even though the AI selects *which* metric/target to query.
- IR structural validity.

### What comes from the user
- The original request.
- All conversational modifications ("make it a gauge," "use the last 6 hours," "add memory as a time series").
- The explicit approval or rejection utterance.

### What belongs to the dashboard specification (IR)
Everything under §11 — dashboard metadata, variables, time config, and the panel list with each panel's query, target, visualization type/config, and layout.

### What is displayed to the user
A proposal card containing: dashboard/operation summary, per-panel title, resolved metric, generated query (human-readable), visualization type, key configuration (unit/thresholds/min-max as applicable), any dashboard variables, and time range. Internal wrapper call details (discovery API calls, resolver internals) are **not** surfaced — only their results are.

---

## 5. Stage 2 — Dashboard Build


Triggered only by an approval event bound to a specific IR version (see §6). Sequence:

```
Approved IR (frozen, versioned)
 → Dashboard Builder: panel JSON generation (per panel, using visualization-specific compiler)
 → Dashboard Builder: dashboard JSON assembly (metadata, variables, time config, panel layout)
 → Validation (schema-level: valid Grafana dashboard/panel JSON)
 → Policy/safety checks (destructive-op checks, quota/limits, forbidden-field checks)
 → Grafana MCP call (create_dashboard / update_dashboard / delete_panel, etc.)
 → Grafana persists/renders
 → Wrapper confirms result back to AI agent → AI agent reports outcome to user
```

The builder is a pure function of the approved IR: `IR → Grafana JSON`. It performs no discovery, no LLM calls, and no interpretation of ambiguous intent — all ambiguity must already be resolved by the time IR is approved. If the builder cannot produce valid JSON from the IR (e.g., an internal inconsistency), it fails closed and returns to Stage 1 rather than guessing.

---

## 6. Human-in-the-Loop Approval Architecture


### Why the gate sits before the MCP write, not after

An after-the-fact approval ("undo if you don't like it") requires Grafana to support atomic, reliable rollback of every operation type, and it means a destructive REMOVE has already executed before the human can react. A before-the-write gate:
- Guarantees Grafana state never changes without a human decision on the *exact* resulting mutation.
- Lets the system show the user the literal diff ("2 panels created," "1 panel deleted") rather than a post-hoc summary of something already done.
- Makes validation and policy checks meaningful — they can block a mutation instead of racing to undo one.

### How the architecture prevents the LLM from bypassing approval

- The LLM has no tool binding to any Grafana-mutating MCP method. Its available tools are limited to read-only discovery/query-generation wrapper calls and proposal-drafting functions.
- The wrapper exposes exactly one mutating entry point — `execute_approved_mutation(ir_version_id)` — and this entry point requires an `approval_token` that is issued only by the approval-state component, only for a specific `ir_version_id`, only in response to an explicit user approval event captured by the frontend (a button press or an unambiguous confirmatory utterance classified with high confidence, not free-text inference by the LLM alone).
- The `ir_version_id` bound to the approval token must match the `ir_version_id` currently at the head of the conversation's proposal history. If the user modified the proposal after approval was requested but before confirming, the stale token is rejected and a new approval is required.
- The AI agent orchestration layer treats "approval" as a state transition owned by the wrapper's approval-state store, not as something the LLM can assert in free text. Even if the LLM's output text says "approved, proceeding," the orchestration layer will not invoke the mutating tool unless the approval-state store independently confirms a valid token.

### Approval UI content
Each approval prompt states the literal mutation, not just the request:
- CREATE: "2 panels will be created on dashboard 'GPU Monitoring'." → [Reject] [Modify] [Approve & Create]
- UPDATE: "1 panel will be added; 0 existing panels modified." → [Reject] [Modify] [Approve & Update]
- REMOVE: "Panel 'GPU Temperature' will be deleted from dashboard 'GPU Monitoring'." → [Cancel] [Approve & Delete]

---

## 7. CREATE Architecture


```
User: "Create a GPU dashboard for node-01."
 → AI: intent = CREATE, target = new dashboard, node = node-01
 → Wrapper: discover GPU metrics → DCGM_FI_DEV_GPU_UTIL
 → Wrapper: resolve target → node-01 validated
 → Wrapper: generate PromQL for utilization
 → AI: propose Panel 1 (GPU Utilization, Time Series, default config)
 → User: "Change it to a gauge and add GPU temperature."
 → AI: update Panel 1 visualization → Gauge; discover temperature metric; propose Panel 2
 → User: "Go ahead."
 → Approval-state: issue approval_token for current ir_version_id
 → Builder: IR → dashboard JSON (2 panels)
 → Validation + policy checks
 → Grafana MCP: create_dashboard
 → Grafana: dashboard created
 → Interactive dashboard returned to user
```

Note: no full dashboard JSON exists anywhere before Stage 2. Stage 1 only ever holds IR.

## 8. UPDATE Architecture


```
User: "Add memory utilization to my GPU dashboard."
 1. AI: identify existing dashboard reference (by name/id, disambiguate if needed)
 2. Wrapper: fetch current dashboard state (read-only) → hydrate into IR
 3. AI: identify requested change (add panel)
 4. Wrapper: discover memory metric
 5. Wrapper: generate query
 6. AI: construct new panel spec
 7. Wrapper/AI: merge new panel into hydrated IR — existing panels retained byte-for-byte in IR form, only the new panel is added
 8. Proposal shown: "1 panel will be added; existing panels unchanged."
 9. User may modify (visualization, title, etc.) before approving
 10. Explicit approval → approval_token bound to this IR version
 11. Builder: IR → dashboard JSON, applied as a targeted panel-add against the existing dashboard (not full replacement)
 12. Validation
 13. Grafana MCP: update_dashboard (merge semantics)
```

The critical guarantee: the wrapper hydrates the *current* Grafana dashboard into IR before any diffing happens, so unrelated panels pass through unchanged rather than being regenerated from the LLM's understanding of them.

## 9. REMOVE Architecture


```
User: "Remove the GPU temperature panel."
 1. Wrapper: fetch current dashboard, hydrate to IR
 2. AI: resolve exact panel reference (disambiguate by title/id if multiple matches)
 3. Proposal: "Panel 'GPU Temperature' will be deleted from dashboard 'GPU Monitoring'."
 4. User: reject / modify (e.g., "actually remove GPU Utilization instead") / approve
 5. Explicit approval → approval_token
 6. Builder: IR with panel removed → dashboard JSON
 7. Validation: resulting dashboard still structurally valid (e.g., no dangling variable references to the removed panel)
 8. Grafana MCP: update_dashboard (remove semantics)
```

No REMOVE proceeds from a bare LLM tool-call decision — the same approval-token mechanism from §6 applies identically to CREATE, UPDATE, and REMOVE. There is exactly one gate implementation shared by all three operations, not three separate ones.

---

## 10. Query Generation Architecture


Query generation is a Stage 1, read-only wrapper responsibility, never Stage 2. Given a resolved metric and resolved target, the wrapper produces a candidate query string (e.g., PromQL for Prometheus, a query DSL for OpenSearch) using existing datasource-resolution logic. This string is:
- Displayed verbatim to the user in the proposal (readable, not obfuscated).
- Stored in the panel's IR node.
- Carried unchanged into Stage 2 — the builder does not regenerate or "improve" the query. If the approved query must change (e.g., because the user edited the panel post-generation), that is a Stage 1 event that produces a new IR version and a new proposal, not a silent Stage 2 rewrite.

Metrics are never assumed to exist; every metric referenced in a proposal has passed through discovery. If discovery fails, see §21.

---

## 11. Dashboard Specification / Intermediate Representation


```
DashboardIR
 ├── name
 ├── description
 ├── datasource
 ├── variables[]           (see §14B)
 ├── timeConfig             { from, to }
 ├── panels[]
 └── approvalState          { version, status: proposed|modified|approved, approvedAt, approvalToken }

PanelIR
 ├── id                     (stable within a proposal thread)
 ├── title
 ├── datasource
 ├── query
 ├── target
 ├── visualizationType
 ├── visualizationConfig    (type-specific, see §13–14)
 ├── layout                 { x, y }
 ├── size                   { w, h }
 └── variableRefs[]         (optional — panel uses one or more dashboard variables)
```

### Why an IR is necessary
It decouples "what the user agreed to" from "what Grafana's schema happens to require this quarter." The LLM and frontend only ever read/write IR — a stable, purpose-built shape — never raw Grafana JSON. This means:
- The LLM cannot introduce Grafana-schema errors, because it never touches Grafana schema.
- Grafana JSON generation is a single, deterministic, testable compiler (`IR → JSON`) with no LLM involvement, so it can be unit-tested independently of any model behavior.
- Grafana API/schema changes are isolated to the compiler and the MCP boundary; the proposal/approval/modification logic upstream is unaffected.
- Incremental edits (§5/§8) are IR-node-level operations (add/replace one PanelIR), not full-document regeneration.

---

## 12. Visualization Architecture


```
query/data
 ↓
PanelIR (metric, target, query — visualization-agnostic)
 ↓
visualizationType selection (user-specified, honored as-is; or AI-proposed, subject to approval)
 ↓
visualizationConfig compiler (one compiler per type)
 ↓
Grafana panel JSON (type-specific schema)
```

Each visualization type has its own config compiler (`TimeSeriesCompiler`, `GaugeCompiler`, `BarChartCompiler`, …) that consumes the generic PanelIR fields plus the type-specific `visualizationConfig` block and emits the Grafana panel JSON for that type. Adding a new visualization type means adding a new compiler and a new config shape — it does not touch metric discovery, target resolution, or query generation, which operate purely on PanelIR's generic fields.

If the user explicitly requests a visualization type, the AI must set `visualizationType` to that value without independently overriding it. If unspecified, the AI proposes a default (informed by metric semantics, e.g., a single current-value metric defaults toward Stat/Gauge, a time-series metric defaults toward Time Series), but this default remains visible and editable pre-approval.

## 13. Visualization-Specific Configuration


| Type | Key config fields |
|---|---|
| Time Series | time axis, legend, units, thresholds, line/area style |
| Stat | value calculation (last/mean/max…), unit, decimals, thresholds |
| Gauge | min, max, unit, thresholds |
| Bar Chart | category/value mapping, orientation, units, legend |
| Table | columns, transformations, formatting |
| Pie Chart | value field, legend, unit |
| Heatmap | bucket configuration, color scale, axes |
| State Timeline | state mapping, time intervals, labels |

This config lives entirely inside `PanelIR.visualizationConfig` and is opaque to every component except the matching compiler. Metric discovery, target resolution, and query generation (§10) never inspect it.

---

## 14. Dashboard Variables and Dropdowns


Two distinct concepts, not to be conflated:

### A. Interactive agent UI controls (Stage 1)
Ephemeral controls in the conversational proposal UI — e.g. a "Visualization: [Time Series ▼]" dropdown next to a panel proposal — that let the user modify the *proposal* before approval. These controls write directly to the in-flight IR and never touch Grafana. They exist only in the AI-agent frontend.

### B. Generated Grafana dashboard variables (Stage 2 output)
A `DashboardIR.variables[]` entry (e.g., `Node`, with allowed values `node-00..node-03`) that the AI proposes when the user asks for something like "let me switch nodes" or when the request implies parameterization across multiple targets. On approval, this compiles into a native Grafana templating variable, and any panel's `variableRefs` causes its compiled query to reference `$node` (e.g., `{instance="$node"}`) instead of a hardcoded target. This is a Grafana-native, post-build, end-user-facing interactive control.

The AI can propose (B) during Stage 1 — the proposal must show the variable name and its available values, sourced from wrapper discovery (not invented) — and it is approved/modified exactly like a panel. Whether a mentor's "dropdown" request means (A) or (B) is disambiguated by context: a request to change something *while building* the dashboard is (A); a request for the *resulting dashboard* to be switchable by end users is (B). When ambiguous, the AI should ask or propose both are covered by the interactive-controls layer and confirm with the user which is intended.

---

## 15. Interactive UI Architecture


Two UI surfaces, clearly separated:

**AI-agent conversational UI** (Stage 1 + approval): renders proposal cards, per-panel query/config text, Stage-1 controls (§14A), approval buttons, and modification affordances. This is where "human review" and "human approval" physically happen.

**Generated Grafana dashboard UI** (Stage 2 output): Grafana's own native rendering — dashboard variables/dropdowns (§14B), time-range picker, panel interactions, legends, thresholds, filtering. The AI agent does not reimplement this; it links/embeds the resulting Grafana dashboard.

These two UIs never share a rendering path. The agent UI operates on IR; the Grafana UI operates on committed Grafana JSON.

---

## 16. Backend / Wrapper Responsibilities


**Responsibility:** metric discovery, label discovery, target/node resolution, query generation/validation, dashboard/panel discovery (hydration for UPDATE/REMOVE), IR management and versioning, deterministic IR→JSON compilation, schema validation, policy/safety checks, approval-token issuance and verification, all Grafana MCP calls.
**Input:** structured requests from the AI/ADK layer (discovery queries, IR mutation requests, approval confirmations).
**Output:** discovery results, generated queries, IR versions, compiled Grafana JSON, MCP call results.
**State:** current IR version per conversation/dashboard thread, approval-state store, hydrated dashboard cache for UPDATE/REMOVE.
**Dependencies:** Grafana MCP, Prometheus/OpenSearch datasources, Grafana.
**Must NOT:** accept a mutation request without a valid approval token bound to the exact IR version being executed; allow the LLM to invoke MCP directly; silently alter an approved IR before compiling it.

## 17. AI / ADK Responsibilities


**Responsibility:** natural-language intent extraction, orchestrating wrapper discovery calls, drafting/updating IR proposals in response to conversation, presenting proposals conversationally, detecting approval-intent utterances and forwarding them to the approval-state component, reporting Stage 2 outcomes back to the user.
**Input:** user messages, wrapper discovery/query results, current IR state.
**Output:** IR mutation requests (add/edit/remove panel, set variable, etc.), natural-language proposal summaries, forwarded approval events.
**State:** conversation history, reference to the active IR version (read/propose only — the canonical copy lives in the wrapper).
**Dependencies:** wrapper's discovery/proposal tool surface.
**Must NOT:** call any Grafana-mutating tool; assert approval on the user's behalf; fabricate metric/target names not returned by discovery.

## 18. Grafana MCP Boundary


**Responsibility:** execute exactly the operation instructed by the wrapper (create/update/delete), against the live Grafana instance, after wrapper-side validation and policy checks have already passed.
**Input:** compiled, validated Grafana JSON + operation type, invoked only via the wrapper's `execute_approved_mutation` entry point.
**Output:** MCP call result (success/failure, resulting dashboard reference).
**State:** none held by MCP itself beyond the call in flight.
**Must NOT:** be called directly by the LLM/AI agent layer; be called without an approval token verified by the wrapper; receive IR — only receives compiled Grafana JSON.

## 19. External Chart-System Integration


Boundary for the teammate's chart/visualization work:

```
Dashboard-generation backend (this feature)
 ↓  structured, approved representation
External chart/visualization layer
 ↓
Interactive UI
```

Information that must cross this boundary (conceptual, not a fixed API contract): dashboard metadata (name, description), the panel list with query, visualization type, and visualization configuration per panel, dashboard variables, layout/size per panel, current IR/approval state (proposed vs. approved vs. built), and a reference to the live Grafana dashboard once built. This feature does not depend on that layer existing — it is independently demonstrable via the AI-agent UI (proposal cards) and the native Grafana dashboard alone; the chart layer is an additional consumer of the same IR/output, not a dependency of the write path.

---

## 20. Validation and Safety


| Level | What it checks | When |
|---|---|---|
| Query validation | Syntactic validity of generated PromQL/query | Stage 1, at generation time |
| Target validation | Target/node actually exists | Stage 1, at resolution time |
| Dashboard spec (IR) validation | Structural completeness/consistency of IR | Before showing proposal, and again before compiling |
| Grafana JSON validation | Schema-valid panel/dashboard JSON | Stage 2, post-compile |
| Mutation/policy validation | Destructive-op limits, quota, forbidden fields, permissions | Stage 2, pre-MCP-call |
| Human approval | User consents to the specific, literal mutation | Stage 1→2 boundary |

Human approval and validation are independent, both mandatory. Approval without passing validation must not write; passing validation without approval must not write. For destructive/significant changes (REMOVE, or UPDATE that replaces existing panels), the approval UI must state the mutation in concrete terms (§6), not a generic "confirm changes."

---

## 21. Failure Handling


All failures are fail-closed: Grafana state is unchanged until a full, validated, approved mutation succeeds atomically at the MCP call.

| Case | Behavior |
|---|---|
| Metric not found | AI reports it, offers closest discovered alternatives if any; no proposal panel created for it |
| Ambiguous metric | AI asks user to disambiguate among discovered candidates; proposal withheld for that panel |
| Target not found | Same pattern as metric-not-found, scoped to target resolution |
| Invalid PromQL | Wrapper rejects at generation; AI reports and retries generation or asks for clarification |
| Unsupported visualization | AI reports type unsupported, offers supported list, does not silently substitute without telling user |
| Incomplete user request | AI asks a clarifying question before assembling a proposal |
| Invalid visualization config | Wrapper validation rejects IR; proposal shown with the invalid field flagged, not silently dropped |
| Dashboard not found (UPDATE/REMOVE) | AI reports, asks user to confirm/search again |
| Panel not found (UPDATE/REMOVE) | Same pattern, scoped to panel |
| Dashboard JSON validation failure (Stage 2) | Build aborted, no MCP call made, user returned to Stage 1 with error explained |
| MCP failure | Reported as failure; no partial dashboard state assumed; user can retry the same approved IR |
| Grafana failure | Same as MCP failure |
| User rejection | IR discarded or kept editable per user choice; no write occurs |
| User modifies proposal | New IR version created; any prior approval token is invalidated |
| User changes requirements after approval, before write | Treated as a new modification — invalidates the pending approval token, returns to Stage 1 |
| LLM generates inconsistent proposal | Wrapper's IR validation catches structural inconsistency before it's shown/approved; if caught only post-approval (compiler-level), Stage 2 aborts and requires re-approval on a corrected IR |

---

