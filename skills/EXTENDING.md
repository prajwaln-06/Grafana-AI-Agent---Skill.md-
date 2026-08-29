# Extending `observability-query-builder`

Audience: maintainers adding metrics, domains, exporters, or datasources to this
skill. Not read by Claude during a conversation.

## Contents

- Mental Model
- Progressive Disclosure: What Lives Where
- Dynamic Label Sourcing
- Adding a Metric to an Existing Domain File
- Adding a New Domain File
- Adding a New Exporter
- Adding a New Datasource
- When Information Belongs in `SKILL.md` vs. a Reference
- When to Split a Reference File
- Structural Patterns to Avoid
- Validation
- Fast Lookup
- Regression Testing
- Change-Impact Relationships
- Version History

## Mental Model

This is one skill, not one skill per exporter or per backend. Every exporter and
every backend shares identical *behavior* — one construction procedure (`SKILL.md`
§6), one output contract (§9), one ambiguity/error taxonomy (§7) — and differs
only in *data*: which metrics exist, what they mean, which semantic dimensions
they vary along, which entity they describe. Adding coverage almost always
means adding reference content, not changing `SKILL.md`'s behavior. Note that
label *keys* are not part of this reference data at all — they are sourced
dynamically from the runtime at query-generation time (`SKILL.md` §5
Principle 9), never documented as a static catalog in a reference file; see
Dynamic Label Sourcing below.

`SKILL.md` is the only place that behavior is defined, and the only file always
loaded. Everything else — exporter overviews, domain files, datasource
fundamentals — is a reference, reached only by following a link written directly
in `SKILL.md` §4's routing table. Every reference that an agent may need to
consult must be directly reachable from `SKILL.md` §4. Cross-references between
reference files may still be used for context, but must not be the sole
discovery path to a required reference.

## Progressive Disclosure: What Lives Where

| Level | Lives in | Contains |
|---|---|---|
| Behavior (all exporters, all backends) | `SKILL.md` | Construction procedure, output contract, error handling, parameter defaults, routing table |
| Backend query mechanics | `references/<backend>-fundamentals.md` | Data model, query shape, functions/clauses, gotchas — never metric meaning |
| Exporter-wide facts | `references/<exporter>/overview.md` | Entity model, Metric Directory, Do-Not-Use boundaries, cross-domain distinctions |
| Domain-specific facts | `references/<exporter>/<domain>.md` | Per-metric definitions, confusable-metric writeups within that domain |
| Downstream execution shape | `references/execution-contract.md` | The post-validation `execution` block — not query construction |

The governing rule, carried over from the pre-migration project and still
correct: **information stays at the lowest level where it is true, and no
lower.** A fact true for every exporter belongs in `SKILL.md`. A fact true for
one exporter's two domains belongs in that exporter's `overview.md`. A fact true
for one metric belongs in that metric's own definition. Do not promote something
to a higher level "for convenience," and do not duplicate a fact at two levels —
if you're about to write the same explanation in two files, one of them is wrong.

## Dynamic Label Sourcing

**Static skill files own semantic/behavioral knowledge. The backend/runtime
owns live mechanical schema information such as Prometheus label keys.** This
is a hard boundary, not a style preference — see `SKILL.md` §5 Principle 9 for
the runtime-facing rule this enforces.

When authoring or editing a reference file:

- **Do document** semantic dimensions: the fact that a metric varies by CPU
  and mode, that a domain's metrics are node-scoped vs. GPU-scoped, that a
  filesystem metric applies to "a filesystem" rather than "the whole node."
  This is knowledge about what a metric *means*, and belongs in Domain
  Fundamentals / Common Labels & Dimensions and in each metric's Additional
  Known Labels field.
- **Do not document** specific label key names (`cluster`, `instance`, `job`,
  `node_id`, `device`, `gpu`, `cpu`, `mode`, or any other) as a verified,
  authoritative catalog. Those are live schema facts that the runtime supplies
  at query-generation time and that may differ across environments — a
  reference file asserting them as fixed is itself a form of fabrication risk,
  the same failure mode the "never invent labels" guardrail has always existed
  to prevent, just with the wrong source of truth.
- **Incidental mentions are fine.** A worked example showing what a returned
  series' labels might look like (see `execution-contract.md`'s illustrative
  JSON), or a one-off reference to a real, already-verified label used in an
  actually-tested query example (see `cpu.md`'s `node_cpu_seconds_total`
  example, which cites `mode="idle"` because that specific query was run
  against a live Prometheus), are not violations. The distinguishing question
  is always: *is this presented as the authoritative runtime schema, or as
  one piece of already-verified evidence?*
- When a metric's field would otherwise read as an empty catalog ("no labels"),
  state explicitly that label keys are sourced dynamically and that none is
  invented — never leave the field blank in a way that could be misread as
  "labels were checked and there are none."

## Adding a Metric to an Existing Domain File

1. Add a row to the parent exporter's `overview.md` Metric Directory (Domain /
   Intent / Metric name / this domain file).
2. Add a `### \`metric_name\`` section to the domain file following
   `assets/templates/domain-reference-template.md`'s per-metric shape exactly —
   every field, including `Additional Known Labels` (document any semantic
   dimension only, never a static label-key list — see Dynamic Label Sourcing
   above; never leave this field blank) and `Query Examples` (state explicitly
   whether an example is verified, or state "No verified query example is
   currently available. Do not invent a literal query example.").
3. If this metric is confusable with an existing metric in the domain, add it to
   (or create) that domain file's Confusable Measurements section — don't create
   a second, differently-worded explanation of the same distinction.
4. If this metric needs a per-metric override of a `SKILL.md` default (a
   different comparison baseline, or — as with an unverified exposed unit — a
   block on query construction entirely) put it in that metric's own
   Metric-Specific Query/Result Semantics field, never as a change to `SKILL.md`.
5. Run `scripts/check_metric_directory.py` (see Validation below).

## Adding a New Domain File

1. Copy `assets/templates/domain-reference-template.md`, fill in Quick Facts and
   Domain Fundamentals, then add each metric per the steps above. Quick Facts'
   `Merged from` field is maintainer provenance, not agent-facing behavior —
   it exists so a future contributor understands *why* metrics are grouped
   into this domain (e.g. `node-exporter/memory.md`'s Quick Facts explains
   that the original vendor "Swap" category was merged into "Memory" because
   it contained only two closely-related metrics) without needing to
   reverse-engineer the reasoning. Fill it in honestly even when the grouping
   seems obvious; read it before assuming an existing domain boundary is
   arbitrary.
2. Add its metrics to the parent exporter's `overview.md` Metric Directory.
3. Add one row to `SKILL.md` §4's routing table linking **directly** to the new
   file. Do not rely on the exporter's `overview.md` to make it discoverable —
   an agent must be able to find this file by reading `SKILL.md` alone.
4. Add at least one regression case to `evals/regression-cases.md` exercising
   correct routing to the new domain.
5. Run `scripts/check_metric_directory.py`.

## Adding a New Exporter

1. Copy `assets/templates/exporter-overview-template.md` into
   `references/<new-exporter>/overview.md`. Fill in Quick Facts, Trigger
   Examples, Do Not Use, and Exporter Fundamentals (entity model — never a
   static label-key catalog; see Dynamic Label Sourcing above).
   **Do not** re-add a Metric Selection Procedure — that procedure is owned once
   by `SKILL.md` §6 Step 3 and applies unchanged to every exporter.
2. Add one or more domain files per "Adding a New Domain File" above.
3. Add one routing-table row for the new `overview.md` and one for each domain
   file, all linking directly from `SKILL.md` §4.
4. If the new exporter uses a backend with no existing fundamentals file, see
   "Adding a New Datasource" first.
5. Add regression cases covering: correct routing to the new exporter (not
   misrouted to an existing one), and at least one metric-selection case.
6. Run `scripts/check_metric_directory.py`.

## Adding a New Datasource

1. Copy `assets/templates/datasource-fundamentals-template.md` into
   `references/<backend>-fundamentals.md`. Document query shape, clauses/
   functions, and gotchas — never metric meaning, which belongs in the
   exporter/domain files that use this backend.
2. Add one routing-table row in `SKILL.md` §4.
3. If no exporter or domain routes to this backend yet, keep the template's
   status banner ("infrastructure only, no implemented consumer") in the file
   and in its routing-table row — see `opensearch-fundamentals.md` for the
   pattern. Do not let the routing table imply metric coverage that doesn't
   exist. Remove the banner only once a real domain file sets this backend as
   its data source.

## When Information Belongs in `SKILL.md` vs. a Reference

Ask: is this true for every exporter and every backend, or only for some subset?

- **True for everything → `SKILL.md`.** The construction procedure, the output
  contract, parameter defaults, error handling. If you're tempted to write "for
  Node Exporter, do X, but for DCGM, do Y" inside `SKILL.md`, that's a sign the
  content belongs in each exporter's reference instead, not in `SKILL.md` with a
  branch.
- **True for one backend's query language → that backend's fundamentals file.**
  Never inside an exporter or domain file.
- **True for one exporter (or shared by ≥2 of its domains) → that exporter's
  `overview.md`.** Not `SKILL.md`, and not copy-pasted into each domain file.
- **True for one domain → that domain file.** Not the exporter overview.
- **True for one metric → that metric's own definition**, including any
  per-metric override of a `SKILL.md` default.

If new content doesn't cleanly satisfy any of these, it's a sign the request is
either out of this skill's scope (see `SKILL.md` §3) or that the content needs
to be narrowed until it's true at one specific level.

## When to Split a Reference File

Split a domain file when it covers metrics that don't share Confusable
Measurements or a common semantic-dimension baseline — i.e., when the file is
really two domains sharing a directory by accident. Don't split purely because a
file crossed 100 lines; a table of contents (required past 100 lines — see
`prometheus-fundamentals.md` or `node-exporter/cpu.md` for the pattern) is the
first response to length, not a split.

Split an exporter's `overview.md` from its domain files only along the existing
line — exporter-wide facts vs. per-domain facts. Never introduce a second
navigation layer between `SKILL.md` and a domain file; every reference stays one
hop from `SKILL.md` regardless of how the exporter's own content is organized
into subdirectories.

## Structural Patterns to Avoid

- **No `_index.md` or `index.md` files.** A reference file's role (exporter
  overview, domain reference, datasource fundamentals) should be evident from
  its content and its routing-table entry, not from a special filename that
  implies it's the only way to find other files.
- **No file that exists only to be a discovery path to another file.** If a
  file's only content is "see also X, Y, Z," add X/Y/Z directly to `SKILL.md`
  §4 instead and delete the intermediary.
- **No YAML frontmatter on reference files.** Only `SKILL.md`'s frontmatter is
  validated against the Agent Skills spec; frontmatter on a reference file
  falsely implies it's an independently discoverable skill. Use a plain
  "Quick Facts" markdown block instead.
- **No restating the same distinction from two angles.** If a domain file has
  both a "why these are confusable" write-up and a separately-worded "semantic
  distinction" write-up covering the same metrics, they should be one section
  (see the Confusable Measurements pattern in `cpu.md`).
- **No promoting exporter-specific procedure to `SKILL.md` "just in case," and
  no re-deriving a general procedure per exporter.** The Metric Selection
  Procedure was duplicated three ways pre-migration precisely because of this;
  it now lives once, in `SKILL.md` §6 Step 3.
- **No silent gap-filling.** If a metric has no verified query example, say so
  explicitly ("No verified query example is currently available. Do not invent
  a literal query example.") rather than leaving the field blank or omitting it.
- **No static label-key catalogs in reference files.** Label keys are sourced
  dynamically from the runtime at query-generation time (`SKILL.md` §5
  Principle 9) — see Dynamic Label Sourcing above. A reference file may
  document semantic dimensions a metric varies along, never a specific list
  of label key names presented as verified schema.

## Validation

Run the maintainer-only consistency checker after any change to a Metric
Directory, a domain file's metric definitions, or `SKILL.md`'s routing table:

```bash
python3 scripts/check_metric_directory.py .
```

It verifies two invariants, neither previously checked by anything but careful
authoring:

1. Every metric in an exporter's Metric Directory has a matching
   `### \`metric_name\`` definition in the domain file it points to, and vice
   versa.
2. Every reference linked from `SKILL.md` §4 exists on disk, unless that
   routing-table row is explicitly marked "pending addition."

A clean run prints `OK: ...` and exits `0`. Any inconsistency is listed with the
specific file and metric involved, and the script exits non-zero — safe to wire
into CI.

## Fast Lookup

Maintainer-only grep starting points (not read by Claude at runtime):

```bash
grep -i "node_memory"      references/node-exporter/memory.md
grep -i "DCGM_FI_PROF"     references/dcgm-exporter/compute.md
grep -i "no verified"      references/**/*.md   # find every metric still missing a verified query example
grep -i "unsupported_metric" references/dcgm-exporter/thermal.md
```

## Regression Testing

`evals/regression-cases.md` holds representative cases (routing, raw-vs-derived
classification, explicit scope, multiple explicit measurements, ambiguity,
unsupported measurements, per-metric unverified-semantics overrides, invented-
identifier rejection, out-of-scope actions, datasource-infrastructure-only
boundaries, panic mode). It is maintainer-run — against a live Claude session
using this skill — not agent-run; Claude is not responsible for judging its own
output's correctness.

Run the full set after any change to `SKILL.md` §5–§9 (behavior) or to any
reference a case depends on. Add a new case whenever you add a domain, exporter,
or datasource, and whenever you fix a bug that regression testing should have
caught — a case that would have caught it, added at the time of the fix, is what
prevents the same bug from returning silently.

## Change-Impact Relationships

| If you change... | Also check |
|---|---|
| `SKILL.md` §6 (Construction Procedure) | Every reference's Metric-Specific Query/Result Semantics still fits the procedure's assumptions; full regression suite |
| `SKILL.md` §9 (Output Contract) | Every regression case's `Expected` field shape; `references/execution-contract.md`'s stated relationship to `time_range` |
| `SKILL.md` §4 (Routing Table) | `scripts/check_metric_directory.py` (link resolution check); no reference file is orphaned or newly unreachable |
| An exporter's `overview.md` Metric Directory | The corresponding domain file(s); `scripts/check_metric_directory.py` |
| A domain file's metric definitions | The parent `overview.md` Metric Directory; any regression case referencing that metric |
| A datasource-fundamentals file | Every exporter/domain file using that `data_source`; the query-construction guidance in `SKILL.md` §6 Step 5 |
| A template in `assets/templates/` | Not retroactive — existing files aren't required to change, but new files should follow the updated template |

## Version History

`SKILL.md`'s own changelog (§11) carries only the current entry going forward
— see it for what's presently true. This section holds the full history,
including the pre-migration architecture's numbering, for provenance.

**Current architecture (this skill):**

* **1.3** — Added the `query_type` output field (§8/§9) so an instant-value
  question resolves to a true Prometheus instant query instead of always
  being forced through a range/matrix query. Rewrote §12.4 so an alert
  rule's `condition_query` is built via the same Step 5 procedure as an
  ordinary read query, instead of requiring a separate, per-metric
  hand-verified "Alert query/threshold" field — alert-rule creation now
  covers every metric this skill can already build a read query for, not
  only `node_cpu_seconds_total`. Removed the "Alert query/threshold" field
  from `assets/templates/domain-reference-template.md` and from every
  existing metric definition.
* **1.2** — Added the narrow, explicit alert-rule-creation exception (§12):
  this skill may PROPOSE creation of a brand-new Grafana alert rule for an
  already-covered metric, subject to a separate, explicit user confirmation
  step this skill never triggers itself. Added the `alert_rule_proposed`
  output status (§9).
* **1.1** — Dynamic-label-sourcing finalization pass: replaced static
  exporter/domain label catalogs with runtime-sourced label keys; added the
  Node Exporter Load and Filesystem extension (`cpu.md` extended,
  `filesystem.md` added); moved maintainer-only content out of `SKILL.md`
  into this document.
* **1.0** — Structural migration to the Agent Skills standard. Consolidated
  the Metric Selection Procedure into `SKILL.md` §6 Step 3. Replaced the
  pre-migration dynamic frontmatter-registry routing mechanism with the
  static routing table in §4. Split the execution-output contract into its
  own reference file.

**Pre-migration architecture (`main_skill.md` and ad-hoc `index.md`/
`_index.md` files, prior to the Agent Skills migration) — numbering restarted
at `1.0` above because this is a structurally different, non-conformant
architecture, not a continuation:**

* 3.2 — Added a strict safeguard preventing invented PromQL queries for
  metrics that lack verified query semantics in their respective references.
  Unverified queries were classified as `unsupported_metric`.
* 3.1 — Added explicit classification rules preventing query transformations
  (mathematical functions) from being flagged as derived measurements.
* 3.0 — Extracted Prometheus and OpenSearch fundamentals to separate files.
  Removed a hardcoded registry table in favor of dynamic per-file delivery
  (later found, during the 1.0 migration, not to correspond to anything an
  Agent Skills implementation actually supports — replaced by the static
  routing table in `SKILL.md` §4). Added the Phase 4 execution output
  contract. Delegated time-range grammar to fundamentals files.
* 2.2 — Generalized "compound" responses into `mode: "multi"`. Fully
  specified output shapes for every non-ok status.
* 2.1 — Rebuilt as an agent-facing operating document.
