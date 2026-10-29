<!--
TEMPLATE — OpenSearch index-family reference file.

Use this for a new index family under references/opensearch-logs/<family>.md.
Fill in every [bracketed] placeholder. Do NOT add YAML frontmatter. Describe
field ROLES and document kinds, not the live mapping: which paths actually exist
and what each can do is confirmed at runtime by the field catalog, and which
fields may be queried at all is authorized by
app/opensearch_catalog/index_registry.json.

After creating this file add one row to SKILL.md §4's routing table, add the
family to references/opensearch-logs/overview.md's family table, and add the
registry entry (see EXTENDING.md "Adding an OpenSearch Index Family").
-->

Defines the [family description] index family (`pattern_id: [pattern_id]`) and
the semantics needed to query it correctly.

## Quick Facts

- **Parent domain:** opensearch-logs ([overview.md](overview.md))
- **`pattern_id`:** `[pattern_id]` (index pattern resolved by the registry)
- **Covers:** [one-line scope]
- **Time field:** `@timestamp` (ingest time) unless the registry says otherwise
- **Caveats that apply:** [codes from overview.md's Shared Data Caveats]

## Routing Triggers

Route here for: [topics]. Do not route here for: [negative examples, naming the
reference they belong to instead].

## Field Roles

| Role | Meaning | Illustrative path |
|---|---|---|
| [role] | [what it means] | [path the registry binds] |

## Document Kinds

[Document kinds that share the index, how to tell them apart, and which one a
typical question is about. State observed value sets as observed, not complete.]

## Query Intent Patterns

| Intent | Approved shape |
|---|---|
| [rows / count / top_n / trend / distinct] | [shape built from roles] |

State which fields are never grouped (free text) and which clause forms are
disallowed (scripts, query_string, regular expressions, leading wildcards).

## Severity and "Error" Definition

[What counts as an error or unhealthy state for this family, stated against the
stored values. Mark anything unconfirmed as pending.]

## Known Data Caveats

[Each applicable caveat code and what an answer must say about it.]

## Safe Projection

[Which role fields rows may return, and what must never be returned.]

## Query Examples

State explicitly whether an example is verified against the live cluster, or
state "No verified query example is currently available." Do not present a
fixture-checked illustration as verified.

## Guardrails

[Short list of hard rules for this family.]
